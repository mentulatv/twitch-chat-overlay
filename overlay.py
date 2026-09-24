"""Click-through Twitch chat overlay for Windows.

Reads chat anonymously over Twitch IRC (no login needed) and draws it in a
borderless, always-on-top, transparent window that mouse clicks pass through.
Twitch, 7TV and BTTV emotes are shown inline (animated ones animate).
Follows, subs, gifts, raids, bits and redemptions show up as a small colored line
with a soft chime (follows/redemptions need a one-time "Login to Twitch").

Settings: .env (channel, optional own Twitch app) and config.json (looks/behaviour), next to the program.
Run with `login` to log in to Twitch, or `--channel NAME` to override the channel once.

Hotkeys (global):
  Ctrl+Shift+F8   test alert (cycles through the alert types)
  Ctrl+Shift+F9   toggle edit mode (drag to move, drag bottom-right corner to resize)
  Ctrl+Shift+F10  hide / show overlay
  Ctrl+Shift+F11  clear chat
  Ctrl+Shift+F12  quit

Games must run in borderless / windowed-fullscreen for any overlay to show on top.
"""

import ctypes
import hashlib
import io
import json
import queue
import random
import re
import socket
import sys
import threading
import time
import tkinter as tk
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from tkinter import font as tkfont

from PIL import Image, ImageSequence, ImageTk

import alerts
import settings
from settings import CACHE_DIR, CONFIG_PATH

DEFAULTS = {
    "x": 20,
    "y": 300,
    "width": 420,
    "height": 520,
    "font_family": "Segoe UI",
    "font_size": 13,
    "font_weight": "bold",
    "outline_px": 2,
    "fade_seconds": 60,        # 0 = messages never disappear
    "max_messages": 60,
    "message_gap": 4,
    "show_badges": True,       # prefix mods/VIPs/subs with a small symbol
    "hide_commands": True,     # skip messages starting with "!"
    "hide_users": ["nightbot", "streamelements", "streamlabs", "moobot", "fossabot"],
    "emotes": True,
    "emote_providers": {"7tv": True, "bttv": True, "ffz": True},
    "emote_height": 0,         # px; 0 = scale with font size
    "emote_refresh_minutes": 15,
    "alerts": True,
    "alert_types": {"follow": True, "sub": True, "gift": True, "raid": True, "bits": True, "redeem": True},
    "alert_sound": True,
    "alert_volume": 0.35,      # 0..1
    "alert_sound_file": "",    # optional .wav to use instead of the built-in chime
    "alert_fade_seconds": 180, # alerts linger longer than chat; 0 = never
}
RUNTIME_KEYS = {"channel", "_demo"}  # lives in .env, never written to config.json

KEY_COLOR = "#010101"  # rendered fully transparent; near-black so text edges blend into a dark outline
EDIT_BG = "#1b1b24"
TWITCH_DEFAULT_COLORS = [
    "#FF0000", "#0000FF", "#00FF00", "#B22222", "#FF7F50", "#9ACD32", "#FF4500",
    "#2E8B57", "#DAA520", "#D2691E", "#5F9EA0", "#1E90FF", "#FF69B4", "#8A2BE2", "#00FF7F",
]
BTTV_ZERO_WIDTH = {"SoSnowy", "IceCold", "SantaHat", "TopHat", "ReinDeer", "CandyCane", "cvMask", "cvHazmat"}
USER_AGENT = "twitch-chat-overlay/1.0"

# --- Win32 ---------------------------------------------------------------

user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0002, 0x0004, 0x4000
WM_HOTKEY = 0x0312
VK_F8, VK_F9, VK_F10, VK_F11, VK_F12 = 0x77, 0x78, 0x79, 0x7A, 0x7B
HK_EDIT, HK_HIDE, HK_CLEAR, HK_QUIT, HK_TEST = 1, 2, 3, 4, 5

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]


def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def hotkey_thread(events: queue.Queue):
    mods = MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
    for hk_id, vk in ((HK_EDIT, VK_F9), (HK_HIDE, VK_F10), (HK_CLEAR, VK_F11), (HK_QUIT, VK_F12), (HK_TEST, VK_F8)):
        if not user32.RegisterHotKey(None, hk_id, mods, vk):
            events.put(("status", f"hotkey Ctrl+Shift+F{vk - 0x6F} is taken by another app"))
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            events.put(("hotkey", msg.wParam))


# --- Twitch IRC ------------------------------------------------------------

IRC_RE = re.compile(r"^(?:@(?P<tags>\S+) )?(?::(?P<prefix>\S+) )?(?P<cmd>\S+)(?: (?P<params>.*))?$")


def unescape_tag(v: str) -> str:
    return (v.replace("\\s", " ").replace("\\:", ";").replace("\\r", "\r")
             .replace("\\n", "\n").replace("\\\\", "\\"))


def parse_irc(line: str):
    m = IRC_RE.match(line)
    if not m:
        return None
    tags = {}
    if m["tags"]:
        for part in m["tags"].split(";"):
            k, _, v = part.partition("=")
            tags[k] = unescape_tag(v)
    params = m["params"] or ""
    trailing = None
    if " :" in params:
        params, trailing = params.split(" :", 1)
    elif params.startswith(":"):
        params, trailing = "", params[1:]
    return {"tags": tags, "prefix": m["prefix"] or "", "cmd": m["cmd"],
            "params": params.split(), "trailing": trailing}


def irc_thread(channel: str, events: queue.Queue):
    channel = channel.lower().lstrip("#")
    backoff = 1
    while True:
        try:
            sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=15)
            sock.settimeout(360)
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
            sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel}\r\n".encode())
            events.put(("status", f"connected to #{channel}"))
            backoff = 1
            buf = b""
            while True:
                data = sock.recv(8192)
                if not data:
                    raise ConnectionError("disconnected")
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        continue
                    msg = parse_irc(line)
                    if not msg:
                        continue
                    if msg["cmd"] == "PRIVMSG":
                        events.put(("chat", msg))
                    elif msg["cmd"] == "USERNOTICE":
                        events.put(("usernotice", msg))
                    elif msg["cmd"] in ("CLEARMSG", "CLEARCHAT"):
                        events.put(("clear", msg))
                    elif msg["cmd"] == "ROOMSTATE" and "room-id" in msg["tags"]:
                        events.put(("room", msg["tags"]["room-id"]))
                    elif msg["cmd"] == "RECONNECT":
                        raise ConnectionError("server asked to reconnect")
        except Exception as e:
            events.put(("status", f"chat connection lost ({e}); retrying in {backoff}s"))
            try:
                sock.close()
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# --- Emotes --------------------------------------------------------------

def http_get(url: str, timeout=10) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url: str):
    try:
        return json.loads(http_get(url))
    except Exception:
        return None


def fetch_third_party_sets(room_id: str, providers: dict) -> dict:
    """name -> spec. When names clash, channel beats global and 7TV > BTTV > FFZ (later add_* wins)."""
    named = {}

    def add_ffz(sets):
        for s in (sets or {}).values():
            for e in s.get("emoticons") or []:
                if e.get("modifier"):  # ffzW / ffzCursed etc. transform other emotes; not drawable alone
                    continue
                urls = e.get("animated") or e.get("urls") or {}  # animated = webp, urls = static png
                url = urls.get("2") or urls.get("1")
                if url:
                    named[e["name"]] = {"name": e["name"], "key": f"ffz_{e['id']}", "url": url, "zero_width": False}

    def add_bttv(emotes):
        for e in emotes or []:
            if e.get("modifier"):
                continue
            named[e["code"]] = {"name": e["code"], "key": f"bttv_{e['id']}", "url": f"https://cdn.betterttv.net/emote/{e['id']}/2x.webp",
                                "zero_width": e["code"] in BTTV_ZERO_WIDTH}

    def add_7tv(emotes):
        for e in emotes or []:
            host = (e.get("data") or {}).get("host") or {}
            base = host.get("url") or f"//cdn.7tv.app/emote/{e['id']}"
            named[e["name"]] = {"name": e["name"], "key": f"7tv_{e['id']}", "url": f"https:{base}/2x.webp",
                                "zero_width": bool(e.get("flags", 0) & 1)}

    ffz_on, bttv_on, seventv_on = (providers.get(k, True) for k in ("ffz", "bttv", "7tv"))
    if ffz_on:
        g = http_json("https://api.frankerfacez.com/v1/set/global") or {}
        add_ffz({str(i): g["sets"][str(i)] for i in g.get("default_sets", []) if str(i) in g.get("sets", {})})
    if bttv_on:
        add_bttv(http_json("https://api.betterttv.net/3/cached/emotes/global"))
    if seventv_on:
        add_7tv((http_json("https://7tv.io/v3/emote-sets/global") or {}).get("emotes"))
    if ffz_on:
        add_ffz((http_json(f"https://api.frankerfacez.com/v1/room/id/{room_id}") or {}).get("sets"))
    if bttv_on:
        bttv = http_json(f"https://api.betterttv.net/3/cached/users/twitch/{room_id}") or {}
        add_bttv((bttv.get("sharedEmotes") or []) + (bttv.get("channelEmotes") or []))
    if seventv_on:
        seventv = http_json(f"https://7tv.io/v3/users/twitch/{room_id}") or {}
        add_7tv((seventv.get("emote_set") or {}).get("emotes"))
    return named


def load_emote_frames(spec: dict, height: int):
    """Download (disk-cached) and decode an emote into RGBA frames scaled to `height`."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / (re.sub(r"[^\w.-]", "_", spec["key"]) + "_" + hashlib.md5(spec["url"].encode()).hexdigest()[:8])
    if path.exists():
        data = path.read_bytes()
    else:
        data = http_get(spec["url"])
        path.write_bytes(data)
    img = Image.open(io.BytesIO(data))
    frames, durations = [], []
    for frame in ImageSequence.Iterator(img):
        f = frame.convert("RGBA")
        w = max(1, round(f.width * height / f.height))
        f = f.resize((w, height), Image.LANCZOS)
        # drop near-invisible pixels so soft shadows don't turn into dark haze on the color key
        a = f.getchannel("A").point(lambda v: 0 if v < 48 else v)
        f.putalpha(a)
        frames.append(f)
        durations.append(max(20, frame.info.get("duration", 100) or 100))
    return frames, durations


# --- Overlay -------------------------------------------------------------

def readable(color: str) -> str:
    """Brighten name colors that would vanish against dark game scenes."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if lum < 90:
        f = 0.55
        r, g, b = (int(c + (255 - c) * f) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def safe_text(s: str) -> str:
    # Tk 8.6 cannot always render characters outside the BMP (most emoji)
    return "".join(c if ord(c) <= 0xFFFF else "□" for c in s)


class Overlay:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.events: queue.Queue = queue.Queue()
        self.messages: list[dict] = []   # oldest first
        self.edit_mode = False
        self.hidden = False
        self.status_text = "connecting..."
        self.room_id = None
        self.third_party: dict = {}      # emote name -> spec
        self.emote_version = 0           # bumped when third_party changes, to re-tokenize messages
        self.emote_images: dict = {}     # key -> {"frames": [PhotoImage], "durs": [...], "total": ms, "w": px}
        self.emote_pending: set = set()
        self.emote_failed: set = set()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.anim_items: list = []       # (canvas item, emote key) for animated emotes currently drawn
        self.anim_frame: dict = {}
        self.alert_status = "alerts off" if not cfg["alerts"] else "follows: starting..."
        self.irc_alerts = alerts.IrcAlerts()
        self.chimes = alerts.Chimes(cfg["alert_volume"], cfg["alert_sound_file"]) if cfg["alert_sound"] else None
        self.test_index = 0

        self.root = tk.Tk()
        self.root.title("Twitch Chat Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY_COLOR)
        self.root.attributes("-transparentcolor", KEY_COLOR)
        self.root.geometry(f"{cfg['width']}x{cfg['height']}+{cfg['x']}+{cfg['y']}")

        self.font = tkfont.Font(family=cfg["font_family"], size=cfg["font_size"], weight=cfg["font_weight"])
        self.small = tkfont.Font(family=cfg["font_family"], size=10, weight="bold")
        self.text_h = self.font.metrics("linespace")
        self.space_w = self.font.measure(" ")
        self.emote_h = cfg["emote_height"] or round(self.text_h * 1.3)
        self.canvas = tk.Canvas(self.root, bg=KEY_COLOR, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.render())
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_motion)
        self.drag = None

        self.root.update_idletasks()
        self.hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        self.apply_click_through(True)

        threading.Thread(target=hotkey_thread, args=(self.events,), daemon=True).start()
        if cfg.get("_demo"):
            import demo
            threading.Thread(target=demo.demo_thread, args=(self.events, cfg["_demo"] == "alerts"),
                             daemon=True).start()
        else:
            threading.Thread(target=irc_thread, args=(cfg["channel"], self.events), daemon=True).start()
            if cfg["alerts"]:
                threading.Thread(target=alerts.eventsub_thread, args=(cfg, self.events), daemon=True).start()

        self.root.after(50, self.pump)
        self.root.after(2000, self.keep_on_top)
        self.root.after(1000, self.expire_tick)
        self.root.after(40, self.animate)
        self.render()

    # window styles ---------------------------------------------------------

    def apply_click_through(self, enabled: bool):
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        style = style | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)
        self.keep_on_top(reschedule=False)

    def keep_on_top(self, reschedule=True):
        # fullscreen-ish games sometimes push themselves above other topmost windows
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        if reschedule:
            self.root.after(2000, self.keep_on_top)

    # event pump ------------------------------------------------------------

    def pump(self):
        changed = False
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "chat":
                    if self.cfg["alerts"] and data["tags"].get("bits"):
                        # no irc_msg: Cheer tokens were stripped, so Twitch emote offsets no longer line up
                        changed |= self.add_alert(self.irc_alerts.from_bits(data))
                    else:
                        changed |= self.add_chat(data)
                elif kind == "usernotice":
                    if self.cfg["alerts"]:
                        changed |= self.add_alert(self.irc_alerts.from_usernotice(data), data)
                elif kind == "alert":
                    changed |= self.add_alert(data)
                elif kind == "alert_status":
                    self.alert_status = data
                    changed = changed or self.edit_mode
                elif kind == "clear":
                    changed |= self.handle_clear(data)
                elif kind == "status":
                    self.status_text = data
                    changed = changed or self.edit_mode
                elif kind == "hotkey":
                    self.handle_hotkey(data)
                elif kind == "room":
                    if data != self.room_id and self.cfg["emotes"]:
                        self.room_id = data
                        self.refresh_emote_sets()
                elif kind == "emote_sets":
                    if data:
                        self.third_party = data
                        self.emote_version += 1
                        changed = True
                elif kind == "emote_img":
                    key, frames, durs = data
                    self.emote_pending.discard(key)
                    self.emote_images[key] = {"frames": [ImageTk.PhotoImage(f) for f in frames], "durs": durs,
                                              "total": sum(durs), "w": frames[0].width}
                    changed = True
                elif kind == "emote_fail":
                    self.emote_pending.discard(data)
                    self.emote_failed.add(data)
        except queue.Empty:
            pass
        if changed:
            self.render()
        self.root.after(50, self.pump)

    def refresh_emote_sets(self):
        room = self.room_id

        def work():
            try:
                self.events.put(("emote_sets", fetch_third_party_sets(room, self.cfg["emote_providers"])))
            except Exception as e:
                self.events.put(("status", f"emote list fetch failed: {e}"))
        self.pool.submit(work)
        mins = self.cfg["emote_refresh_minutes"]
        if mins:
            self.root.after(int(mins * 60_000), lambda: room == self.room_id and self.refresh_emote_sets())

    def request_emote(self, spec):
        key = spec["key"]
        if key in self.emote_images or key in self.emote_pending or key in self.emote_failed:
            return
        self.emote_pending.add(key)

        def work():
            try:
                frames, durs = load_emote_frames(spec, self.emote_h)
                self.events.put(("emote_img", (key, frames, durs)))
            except Exception:
                self.events.put(("emote_fail", key))
        self.pool.submit(work)

    def add_chat(self, msg) -> bool:
        tags = msg["tags"]
        login = msg["prefix"].split("!", 1)[0]
        text = msg["trailing"] or ""
        if login.lower() in {u.lower() for u in self.cfg["hide_users"]}:
            return False
        if self.cfg["hide_commands"] and text.startswith("!"):
            return False
        action = text.startswith("\x01ACTION ") and text.endswith("\x01")
        if action:
            text = text[8:-1]
        name = tags.get("display-name") or login
        color = tags.get("color") or TWITCH_DEFAULT_COLORS[sum(map(ord, login)) % len(TWITCH_DEFAULT_COLORS)]
        badge = ""
        if self.cfg["show_badges"]:
            badges = tags.get("badges", "")
            if "broadcaster" in badges:
                badge = "★ "   # star
            elif "moderator" in badges:
                badge = "⚔ "   # swords
            elif "vip" in badges:
                badge = "♦ "   # diamond
        self.messages.append({
            "id": tags.get("id"), "login": login.lower(), "name": badge + name,
            "color": readable(color), "text": text, "action": action, "time": time.time(),
            "twitch_emotes": self.parse_twitch_emotes(tags.get("emotes", ""), len(text)),
            "tokens": None, "tokens_ver": -1,
        })
        del self.messages[:-self.cfg["max_messages"]]
        return True

    @staticmethod
    def parse_twitch_emotes(tag: str, text_len: int):
        """'25:0-4,12-16/1902:6-10' -> sorted [(start, end_inclusive, id)]"""
        out = []
        for part in filter(None, tag.split("/")):
            eid, _, ranges = part.partition(":")
            for r in ranges.split(","):
                a, _, b = r.partition("-")
                if a.isdigit() and b.isdigit() and int(b) < text_len:
                    out.append((int(a), int(b), eid))
        return sorted(out)

    def add_alert(self, alert, irc_msg=None) -> bool:
        if not alert or not self.cfg["alert_types"].get(alert["kind"], True):
            return False
        icon, color = alerts.STYLES[alert["kind"]]
        text = alert.get("message") or ""
        emote_tag = irc_msg["tags"].get("emotes", "") if irc_msg else ""
        self.messages.append({
            "id": irc_msg["tags"].get("id") if irc_msg else None, "login": "", "name": "",
            "color": color, "text": text, "action": False, "time": time.time(),
            "alert": f"{icon} {alert['text']}", "ttl": self.cfg["alert_fade_seconds"],
            "twitch_emotes": self.parse_twitch_emotes(emote_tag, len(text)) if emote_tag else [],
            "tokens": None, "tokens_ver": -1,
        })
        del self.messages[:-self.cfg["max_messages"]]
        if self.chimes:
            self.chimes.play(alert["kind"])
        return True

    def test_alert(self):
        samples = [
            {"kind": "follow", "text": "TestViewer followed", "message": ""},
            {"kind": "sub", "text": "TestViewer subscribed at Tier 1. They've subscribed for 3 months!",
             "message": "love the streams"},
            {"kind": "gift", "text": "TestViewer is gifting 5 Tier 1 Subs to the community!", "message": ""},
            {"kind": "raid", "text": "12 raiders from TestStreamer have joined!", "message": ""},
            {"kind": "bits", "text": "TestViewer cheered 100 bits", "message": "here you go"},
            {"kind": "redeem", "text": "TestViewer redeemed Hydrate!", "message": ""},
        ]
        self.add_alert(samples[self.test_index % len(samples)])
        self.test_index += 1
        self.render()

    def handle_clear(self, msg) -> bool:
        before = len(self.messages)
        if msg["cmd"] == "CLEARMSG":
            target = msg["tags"].get("target-msg-id")
            self.messages = [m for m in self.messages if m["id"] != target]
        elif msg["trailing"]:  # ban/timeout of one user
            user = msg["trailing"].lower()
            self.messages = [m for m in self.messages if m["login"] != user]
        else:  # /clear
            self.messages = []
        return len(self.messages) != before

    def handle_hotkey(self, hk):
        if hk == HK_EDIT:
            self.edit_mode = not self.edit_mode
            self.apply_click_through(not self.edit_mode)
            if not self.edit_mode:
                self.save_geometry()
            self.render()
        elif hk == HK_HIDE:
            self.hidden = not self.hidden
            if self.hidden:
                self.root.withdraw()
            else:
                self.root.deiconify()
                self.root.update_idletasks()
                self.hwnd = user32.GetAncestor(self.root.winfo_id(), 2)
                self.apply_click_through(not self.edit_mode)
        elif hk == HK_TEST:
            self.test_alert()
        elif hk == HK_CLEAR:
            self.messages = []
            self.render()
        elif hk == HK_QUIT:
            if self.edit_mode:
                self.save_geometry()
            self.root.destroy()

    def expire_tick(self):
        now = time.time()

        def alive(m):
            ttl = m.get("ttl", self.cfg["fade_seconds"])
            return not ttl or now - m["time"] < ttl
        kept = [m for m in self.messages if alive(m)]
        if len(kept) != len(self.messages):
            self.messages = kept
            self.render()
        self.root.after(1000, self.expire_tick)

    # message layout --------------------------------------------------------

    def tokenize(self, m):
        """Message -> [("text", str, color, space_before) | ("emote", spec, space_before)]"""
        if m["tokens_ver"] == self.emote_version:
            return m["tokens"]
        body_color = m["color"] if m["action"] else "#ffffff"
        if m.get("alert"):
            tokens = [("text", w, m["color"], i > 0) for i, w in enumerate(m["alert"].split())]
        else:
            tokens = [("text", m["name"] + ("" if m["action"] else ":"), m["color"], False)]
        text = m["text"]
        segments, pos = [], 0   # split text around Twitch-native emote ranges
        for a, b, eid in m["twitch_emotes"]:
            if a < pos:
                continue
            segments.append(("plain", text[pos:a]))
            segments.append(("twitch", eid))
            pos = b + 1
        segments.append(("plain", text[pos:]))
        for kind, val in segments:
            if kind == "twitch":
                spec = {"key": f"twitch_{val}", "zero_width": False,
                        "url": f"https://static-cdn.jtvnw.net/emoticons/v2/{val}/default/dark/2.0"}
                tokens.append(("emote", spec, True))
                continue
            for word in val.split():
                spec = self.third_party.get(word) if self.cfg["emotes"] else None
                if spec:
                    tokens.append(("emote", spec, True))
                else:
                    tokens.append(("text", word, body_color, True))
        m["tokens"], m["tokens_ver"] = tokens, self.emote_version
        return tokens

    def layout(self, m, wrap):
        """Returns (lines, height); each line = (height, [placed atoms])."""
        lines, cur, x = [], [], 0

        def newline():
            nonlocal cur, x
            if cur:
                lines.append(cur)
            cur, x = [], 0

        prev_emote = None
        for tok in self.tokenize(m):
            if tok[0] == "emote":
                spec = tok[1]
                img = self.emote_images.get(spec["key"]) if self.cfg["emotes"] else None
                if img is None and self.cfg["emotes"]:
                    self.request_emote(spec)
                if img is not None:
                    if spec["zero_width"] and prev_emote is not None and prev_emote in cur:
                        cur.append({"t": "emote", "key": spec["key"], "img": img,
                                    "x": prev_emote["x"] + (prev_emote["w"] - img["w"]) // 2, "w": 0})
                        continue
                    sp = self.space_w if tok[2] and x else 0
                    if x and x + sp + img["w"] > wrap:
                        newline()
                        sp = 0
                    atom = {"t": "emote", "key": spec["key"], "img": img, "x": x + sp, "w": img["w"]}
                    cur.append(atom)
                    prev_emote = atom
                    x += sp + img["w"]
                    continue
                # not loaded (yet) or failed: fall back to its name as text
                tok = ("text", spec_name(tok[1], m), "#bbbbbb", tok[2])
            _, word, color, space_before = tok
            prev_emote = None
            word = safe_text(word)
            while word:
                sp = self.space_w if space_before and x else 0
                w = self.font.measure(word)
                if x and x + sp + w > wrap:
                    newline()
                    continue
                if w > wrap:  # single word longer than the whole line: hard-break it
                    cut = len(word)
                    while cut > 1 and self.font.measure(word[:cut]) > wrap:
                        cut -= 1
                    piece, word = word[:cut], word[cut:]
                    cur.append({"t": "text", "s": piece, "color": color, "x": 0, "w": self.font.measure(piece),
                                "sp": 0})
                    x = wrap
                    continue
                cur.append({"t": "text", "s": word, "color": color, "x": x + sp, "w": w, "sp": sp})
                x += sp + w
                word = ""
        newline()
        out, total = [], 0
        for line in lines:
            lh = max([self.text_h] + [self.emote_h for a in line if a["t"] == "emote"])
            out.append((lh, line))
            total += lh
        return out, total

    # drawing ---------------------------------------------------------------

    def outlined_text(self, x, y, text, fill, font, width=None, anchor="nw"):
        c = self.canvas
        o = self.cfg["outline_px"]
        kw = {"anchor": anchor, "font": font}
        if width:
            kw["width"] = width
        for dx in range(-o, o + 1):
            for dy in range(-o, o + 1):
                if dx or dy:
                    c.create_text(x + dx, y + dy, text=text, fill="#000000", **kw)
        return c.create_text(x, y, text=text, fill=fill, **kw)

    def draw_message(self, lines, top, pad):
        for lh, line in lines:
            mid = top + lh // 2
            run = None  # merge neighbouring same-colored words into one outlined text item
            for a in line + [None]:
                if a is not None and a["t"] == "text" and run and run["color"] == a["color"] \
                        and a["x"] == run["end"] + a["sp"]:
                    run["s"] += " " * (1 if a["sp"] else 0) + a["s"]
                    run["end"] = a["x"] + a["w"]
                    continue
                if run:
                    self.outlined_text(pad + run["x"], mid, run["s"], run["color"], self.font, anchor="w")
                    run = None
                if a is None:
                    break
                if a["t"] == "text":
                    run = {"s": a["s"], "color": a["color"], "x": a["x"], "end": a["x"] + a["w"]}
                else:
                    img = a["img"]
                    item = self.canvas.create_image(pad + a["x"], mid, image=img["frames"][0], anchor="w")
                    if len(img["frames"]) > 1:
                        self.anim_items.append((item, a["key"]))
            top += lh

    def render(self):
        c = self.canvas
        c.delete("all")
        self.anim_items = []
        self.anim_frame = {}
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10:
            return
        pad = 8
        wrap = w - pad * 2
        c.configure(bg=EDIT_BG if self.edit_mode else KEY_COLOR)

        if self.edit_mode:
            c.create_rectangle(1, 1, w - 2, h - 2, outline="#9146FF", width=2)
            for i in range(3):  # resize grip
                c.create_line(w - 6 - i * 6, h - 4, w - 4, h - 6 - i * 6, fill="#9146FF", width=2)
            emote_info = f"{len(self.third_party)} 7TV/BTTV/FFZ emotes loaded" if self.third_party else "emotes: loading..."
            self.outlined_text(pad, pad, f"EDIT MODE — #{self.cfg['channel']}\n"
                               "drag to move · drag corner to resize\n"
                               "Ctrl+Shift+F9 to lock · F8 test alert · F12 quits\n" + self.status_text + "\n"
                               + emote_info + "\n" + self.alert_status,
                               "#d7c7ff", self.small, width=wrap)

        y = h - pad
        top_limit = 120 if self.edit_mode else 0
        for m in reversed(self.messages):
            lines, mh = self.layout(m, wrap)
            y -= mh
            if y < top_limit:
                break
            self.draw_message(lines, y, pad)
            if m.get("alert"):
                c.create_rectangle(1, y + 2, 4, y + mh - 2, fill=m["color"], outline="#000000")
            y -= self.cfg["message_gap"]

    def animate(self):
        if self.anim_items:
            now = int(time.monotonic() * 1000)
            for item, key in self.anim_items:
                img = self.emote_images[key]
                t = now % img["total"]
                idx = 0
                for idx, d in enumerate(img["durs"]):
                    if t < d:
                        break
                    t -= d
                if self.anim_frame.get(item) != idx:
                    self.anim_frame[item] = idx
                    self.canvas.itemconfigure(item, image=img["frames"][idx])
        self.root.after(30, self.animate)

    # edit-mode mouse handling -----------------------------------------------

    def in_grip(self, e):
        return e.x > self.canvas.winfo_width() - 24 and e.y > self.canvas.winfo_height() - 24

    def on_motion(self, e):
        if self.edit_mode:
            self.canvas.configure(cursor="size_nw_se" if self.in_grip(e) else "fleur")

    def on_press(self, e):
        if not self.edit_mode:
            return
        self.drag = {"resize": self.in_grip(e), "sx": e.x_root, "sy": e.y_root,
                     "x": self.root.winfo_x(), "y": self.root.winfo_y(),
                     "w": self.root.winfo_width(), "h": self.root.winfo_height()}

    def on_drag(self, e):
        d = self.drag
        if not (self.edit_mode and d):
            return
        dx, dy = e.x_root - d["sx"], e.y_root - d["sy"]
        if d["resize"]:
            self.root.geometry(f"{max(200, d['w'] + dx)}x{max(150, d['h'] + dy)}")
        else:
            self.root.geometry(f"+{d['x'] + dx}+{d['y'] + dy}")

    def on_release(self, e):
        self.drag = None

    def save_geometry(self):
        if self.cfg.get("_demo"):
            return
        self.cfg.update(x=self.root.winfo_x(), y=self.root.winfo_y(),
                        width=self.root.winfo_width(), height=self.root.winfo_height())
        save_config(self.cfg)

    def run(self):
        self.root.mainloop()


def spec_name(spec: dict, m: dict) -> str:
    """Original text for an emote that couldn't be drawn."""
    if spec["key"].startswith("twitch_"):
        eid = spec["key"][7:]
        for a, b, e in m["twitch_emotes"]:
            if e == eid:
                return m["text"][a:b + 1]
        return "?"
    return spec.get("name", "?")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    saved = {}
    if CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # older versions kept the channel in config.json; move it to .env
    old_channel = saved.pop("channel", None)
    saved.pop("twitch_env_file", None)
    if old_channel and not settings.load_env().get("TWITCH_CHANNEL"):
        settings.save_env({"TWITCH_CHANNEL": old_channel})
    cfg.update(saved)
    save_config(cfg)  # writes any newly added settings into an existing config
    return cfg


def save_config(cfg: dict):
    data = {k: v for k, v in cfg.items() if k not in RUNTIME_KEYS}
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def ask_channel() -> str:
    """First-run prompt; the answer is saved to .env."""
    root = tk.Tk()
    root.title("Twitch Chat Overlay")
    root.configure(bg="#18181b", padx=28, pady=22)
    root.resizable(False, False)
    tk.Label(root, text="Which Twitch channel's chat should be shown?", bg="#18181b", fg="#efeff1",
             font=("Segoe UI", 11)).pack(anchor="w")
    tk.Label(root, text="Usually your own Twitch username. You can change it later in the .env file.",
             bg="#18181b", fg="#adadb8", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 10))
    entry = tk.Entry(root, font=("Segoe UI", 13), bg="#0e0e10", fg="#efeff1", insertbackground="#efeff1",
                     relief="flat", highlightthickness=2, highlightcolor="#9146ff", highlightbackground="#3a3a3d")
    entry.pack(fill="x", ipady=4)
    want_login = tk.BooleanVar(value=True)
    tk.Checkbutton(root, text="Also show follows & channel-point redeems (opens a Twitch login)",
                   variable=want_login, bg="#18181b", fg="#efeff1", selectcolor="#0e0e10",
                   activebackground="#18181b", activeforeground="#efeff1",
                   font=("Segoe UI", 9)).pack(anchor="w", pady=(10, 0))
    result = {"name": "", "login": False}

    def ok(_=None):
        result["name"], result["login"] = entry.get(), want_login.get()
        root.destroy()
    tk.Button(root, text="Start overlay", command=ok, font=("Segoe UI", 10, "bold"), bg="#9146ff", fg="white",
              activebackground="#772ce8", activeforeground="white", relief="flat", padx=14, pady=6).pack(pady=(14, 0))
    entry.bind("<Return>", ok)
    # make sure the prompt isn't hidden behind whatever window had focus
    root.attributes("-topmost", True)
    root.after(800, lambda: root.attributes("-topmost", False))
    root.after(50, entry.focus_force)
    root.mainloop()
    name = result["name"].strip().lower().lstrip("#").split("/")[-1]  # also accepts a twitch.tv/name link
    if name:
        settings.save_env({"TWITCH_CHANNEL": name})
        if result["login"]:
            launch_login()
    return name


def launch_login():
    """Login window runs as its own process so it doesn't share the overlay's Tk loop."""
    import subprocess
    cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
    subprocess.Popen(cmd + ["login"])


def already_running() -> bool:
    # a second copy would fight over the global hotkeys
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW(None, False, "Local\\TwitchChatOverlay")  # handle lives until the process exits
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main():
    args = sys.argv[1:]
    set_dpi_aware()
    if args[:1] == ["login"]:
        alerts.login_window()
        return
    if "--demo" in args:  # README screenshots: scripted chat, runs alongside a real copy
        cfg = load_config()
        cfg.update(channel="demo", alert_sound=False, _demo="alerts" if "--alerts" in args else "chat")
        if "--geometry" in args:  # WxH+X+Y
            w, h, x, y = map(int, re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", args[args.index("--geometry") + 1]).groups())
            cfg.update(width=w, height=h, x=x, y=y)
        overlay = Overlay(cfg)
        if "--edit" in args:
            overlay.root.after(3500, lambda: overlay.handle_hotkey(HK_EDIT))
        overlay.run()
        return
    if already_running():
        return
    if not settings.ENV_PATH.exists():
        settings.save_env({})
    cfg = load_config()
    channel = settings.load_env().get("TWITCH_CHANNEL", "")
    if "--channel" in args and args.index("--channel") + 1 < len(args):
        channel = args[args.index("--channel") + 1]
    elif args and not args[0].startswith("-"):
        channel = args[0]  # old style: overlay.py <channel>
    if not channel:
        channel = ask_channel()
        if not channel:
            return
    cfg["channel"] = channel.lower().lstrip("#")
    Overlay(cfg).run()


if __name__ == "__main__":
    main()
