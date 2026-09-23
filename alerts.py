"""Low-key follow / sub / raid / bits / redemption notifications for the chat overlay.

Subs, gifts, raids and bits arrive over the anonymous chat connection (USERNOTICE /
bits-tagged PRIVMSG). Follows and channel-point redemptions only exist on EventSub,
which needs a one-time "Login to Twitch" through the overlay's public Twitch app
(Device Code Flow: you approve in the browser, no password is ever typed here).
The token lives in .tokens.json next to the program.
"""

import json
import math
import queue
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import webbrowser
import winsound
from pathlib import Path

import websocket

import settings
from settings import CACHE_DIR, TOKEN_PATH

ID_BASE = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
EVENTSUB_WS = "wss://eventsub.wss.twitch.tv/ws"
SCOPES = ["moderator:read:followers", "channel:read:redemptions"]

# kind -> (icon, accent color). BMP-only symbols: Tk 8.6 can't draw most emoji.
STYLES = {
    "follow": ("\u2665", "#c9a7ff"),   # heart
    "sub":    ("\u2605", "#ffd37a"),   # star
    "gift":   ("\u2605", "#ffb86b"),
    "raid":   ("\u2691", "#7fd4ff"),   # flag
    "bits":   ("\u25c6", "#8ee6b0"),   # diamond
    "redeem": ("\u2726", "#9fc2ff"),   # four-point star
}

SUB_MSG_IDS = {"sub", "resub", "giftpaidupgrade", "anongiftpaidupgrade", "primepaidupgrade",
               "standardpayforward", "communitypayforward"}
GIFT_MSG_IDS = {"subgift", "anonsubgift", "submysterygift", "anonsubmysterygift"}


# --- credentials / tokens -------------------------------------------------

def app_credentials():
    env = settings.load_env()
    if env.get("TWITCH_CLIENT_ID"):  # the user's own app (secret only applies to it)
        return env["TWITCH_CLIENT_ID"], env.get("TWITCH_CLIENT_SECRET", "")
    return settings.DEFAULT_CLIENT_ID, ""


def post_form(url, params):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def save_tokens(body):
    record = {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_at": time.time() + body.get("expires_in", 14400) - 60,
        "scope": body.get("scope", []),
    }
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(TOKEN_PATH)  # atomic: refresh tokens are single-use
    return record


def device_flow(client_id, secret, on_code):
    """Device Code Flow: the user approves in their browser, we never see a password.
    Calls on_code(user_code, url) once, then blocks until approved. Raises on failure."""
    status, start = post_form(f"{ID_BASE}/device", {"client_id": client_id, "scopes": " ".join(SCOPES)})
    if status != 200:
        raise RuntimeError(f"Twitch rejected the Client ID ({status}): {start.get('message', start)}")
    on_code(start["user_code"], start["verification_uri"])
    interval, deadline = start.get("interval", 5), time.time() + start.get("expires_in", 1800)
    while time.time() < deadline:
        time.sleep(interval)
        params = {"client_id": client_id, "scopes": " ".join(SCOPES), "device_code": start["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
        if secret:
            params["client_secret"] = secret
        status, body = post_form(f"{ID_BASE}/token", params)
        if status == 200 and "access_token" in body:
            save_tokens(body)
            return
        msg = str(body.get("message", "")).lower()
        if "pending" in msg:
            continue
        if "slow_down" in msg:
            interval += 5
            continue
        raise RuntimeError(f"login failed ({status}): {body.get('message', body)}")
    raise RuntimeError("login timed out - try again")


def login_window():
    """Small window that walks the user through the Twitch device login."""
    import threading
    import tkinter as tk

    client_id, secret = app_credentials()
    root = tk.Tk()
    root.title("Login to Twitch - Chat Overlay")
    root.configure(bg="#18181b", padx=28, pady=22)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.after(800, lambda: root.attributes("-topmost", False))
    fg, accent = "#efeff1", "#bf94ff"
    info = tk.Label(root, bg="#18181b", fg=fg, font=("Segoe UI", 11), justify="left", wraplength=380)
    info.pack(anchor="w")
    code = tk.Label(root, bg="#18181b", fg=accent, font=("Consolas", 28, "bold"))
    code.pack(pady=12)
    button = tk.Button(root, text="Open Twitch", font=("Segoe UI", 10, "bold"), bg="#9146ff", fg="white",
                       activebackground="#772ce8", activeforeground="white", relief="flat", padx=14, pady=6)
    button.pack()
    state = {"url": None}
    button.configure(command=lambda: state["url"] and webbrowser.open(state["url"]))

    info.configure(text="Contacting Twitch...")

    def show_code(user_code, url):
        state["url"] = url
        root.after(0, lambda: (info.configure(text="Log in as the channel owner (or a mod), then confirm this code "
                                                   "on the Twitch page that just opened:"),
                               code.configure(text=user_code)))
        webbrowser.open(url)

    def work():
        try:
            device_flow(client_id, secret, show_code)
            root.after(0, lambda: (info.configure(text="Logged in! Follow and channel-point alerts will start "
                                                       "within a minute. You can close this window."),
                                   code.configure(text="✓", fg="#8ee6b0"),
                                   button.configure(text="Close", command=root.destroy)))
        except Exception as e:
            err = str(e)
            root.after(0, lambda: (info.configure(text=err), code.configure(text=""),
                                   button.configure(text="Close", command=root.destroy)))

    threading.Thread(target=work, daemon=True).start()
    root.mainloop()


class TokenProvider:
    def __init__(self):
        self.client_id, self.secret = app_credentials()

    def available(self):
        return bool(self.client_id) and TOKEN_PATH.exists()

    def get(self, force=False):
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        if force or time.time() >= data.get("expires_at", 0):
            params = {"client_id": self.client_id, "grant_type": "refresh_token",
                      "refresh_token": data["refresh_token"]}
            if self.secret:
                params["client_secret"] = self.secret
            status, body = post_form(f"{ID_BASE}/token", params)
            if status != 200 or "access_token" not in body:
                raise PermissionError(f"Twitch login expired ({status}); run Login to Twitch again")
            data = save_tokens(body)
        return data["access_token"]


def helix(tokens: TokenProvider, method, path, body=None, retry=True):
    req = urllib.request.Request(f"{HELIX}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Client-Id": tokens.client_id,
                                          "Authorization": f"Bearer {tokens.get()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            tokens.get(force=True)
            return helix(tokens, method, path, body, retry=False)
        raise


# --- EventSub (follows, redemptions) ------------------------------------

def eventsub_thread(cfg: dict, events: queue.Queue):
    types = cfg["alert_types"]
    if not (types.get("follow") or types.get("redeem")):
        return
    tokens = TokenProvider()
    while not tokens.available():  # re-checked every minute so logging in later needs no restart
        events.put(("alert_status", "follows: not logged in (run Login to Twitch)"))
        time.sleep(60)
        tokens = TokenProvider()

    backoff = 2
    while True:
        try:
            me = helix(tokens, "GET", "/users")["data"][0]
            chan = helix(tokens, "GET", f"/users?login={urllib.parse.quote(cfg['channel'].lower())}")["data"][0]
            url, subscribe = EVENTSUB_WS, True
            while True:  # one iteration per socket; session_reconnect moves to a new url
                ws = websocket.create_connection(url, timeout=15)
                try:
                    welcome = json.loads(ws.recv())
                    session = welcome["payload"]["session"]
                    ws.settimeout(session.get("keepalive_timeout_seconds", 10) + 15)
                    if subscribe:
                        ok = subscribe_all(tokens, session["id"], chan["id"], me["id"], types)
                        events.put(("alert_status", f"follows/redeems: listening ({', '.join(ok) or 'nothing allowed'})"))
                    backoff = 2
                    url = None
                    while url is None:
                        frame = json.loads(ws.recv())
                        mtype = frame["metadata"]["message_type"]
                        if mtype == "notification":
                            alert = eventsub_to_alert(frame["payload"]["subscription"]["type"], frame["payload"]["event"])
                            if alert and types.get(alert["kind"], True):
                                events.put(("alert", alert))
                        elif mtype == "session_reconnect":
                            url, subscribe = frame["payload"]["session"]["reconnect_url"], False
                        elif mtype == "revocation":
                            events.put(("alert_status", f"eventsub revoked: {frame['payload']['subscription']['type']}"))
                finally:
                    ws.close()
        except PermissionError as e:
            events.put(("alert_status", str(e)))
            time.sleep(300)
        except Exception as e:
            events.put(("alert_status", f"follows: reconnecting ({type(e).__name__}) in {backoff}s"))
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


def subscribe_all(tokens, session_id, broadcaster_id, my_id, types):
    plan = []
    if types.get("follow"):
        plan.append(("channel.follow", "2", {"broadcaster_user_id": broadcaster_id, "moderator_user_id": my_id}))
    if types.get("redeem"):
        plan.append(("channel.channel_points_custom_reward_redemption.add", "1", {"broadcaster_user_id": broadcaster_id}))
    ok = []
    for typ, ver, cond in plan:
        try:
            helix(tokens, "POST", "/eventsub/subscriptions", {
                "type": typ, "version": ver, "condition": cond,
                "transport": {"method": "websocket", "session_id": session_id}})
            ok.append("follows" if typ == "channel.follow" else "redeems")
        except urllib.error.HTTPError:
            pass  # e.g. redemptions need the broadcaster's own token / affiliate
    return ok


def eventsub_to_alert(sub_type, ev):
    if sub_type == "channel.follow":
        return {"kind": "follow", "text": f"{ev['user_name']} followed", "message": ""}
    if sub_type == "channel.channel_points_custom_reward_redemption.add":
        return {"kind": "redeem", "text": f"{ev['user_name']} redeemed {ev['reward']['title']}",
                "message": ev.get("user_input") or ""}
    return None


# --- IRC-sourced alerts (no login needed) ---------------------------------

class IrcAlerts:
    """Turns USERNOTICE / bits PRIVMSG into alerts, collapsing gift bombs to one line."""

    def __init__(self):
        self.pending_gifts: dict[str, int] = {}

    def from_usernotice(self, msg):
        tags = msg["tags"]
        mid = tags.get("msg-id", "")
        gifter = tags.get("login", "")
        system = tags.get("system-msg", "").strip()
        if mid in ("submysterygift", "anonsubmysterygift"):
            n = int(tags.get("msg-param-mass-gift-count", "0") or 0)
            self.pending_gifts[gifter] = self.pending_gifts.get(gifter, 0) + n
        elif mid in ("subgift", "anonsubgift") and self.pending_gifts.get(gifter, 0) > 0:
            self.pending_gifts[gifter] -= 1  # already announced as part of the bomb
            return None
        if mid in SUB_MSG_IDS:
            kind = "sub"
        elif mid in GIFT_MSG_IDS:
            kind = "gift"
        elif mid == "raid":
            kind = "raid"
        else:
            return None
        return {"kind": kind, "text": system or mid, "message": msg.get("trailing") or ""}

    @staticmethod
    def from_bits(msg):
        tags = msg["tags"]
        name = tags.get("display-name") or msg["prefix"].split("!", 1)[0]
        bits = tags.get("bits", "0")
        text = msg.get("trailing") or ""
        # drop the Cheer100-style tokens; the line already says how many bits
        text = " ".join(w for w in text.split() if not (w[-1:].isdigit() and w.rstrip("0123456789").isalpha()))
        return {"kind": "bits", "text": f"{name} cheered {bits} bit{'s' if bits != '1' else ''}", "message": text}


# --- sound ----------------------------------------------------------------

def make_chime(path: Path, notes, volume: float):
    """Soft sine 'ding' with a quick attack and exponential decay."""
    rate, dur, gap = 44100, 0.55, 0.11
    total = int(rate * (dur + gap * (len(notes) - 1)))
    samples = [0.0] * total
    for i, freq in enumerate(notes):
        start = int(rate * gap * i)
        for n in range(int(rate * dur)):
            t = n / rate
            env = min(1.0, t / 0.008) * math.exp(-t * 7.5)
            v = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t)
            samples[start + n] += v * env * 0.8
    peak = max(abs(s) for s in samples) or 1
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(s / peak * volume * 32767)) for s in samples))


class Chimes:
    def __init__(self, volume: float, custom_file: str = ""):
        CACHE_DIR.mkdir(exist_ok=True)
        vol = max(0.0, min(1.0, volume))
        self.files = {}
        if custom_file and Path(custom_file).exists():
            self.files = {k: str(custom_file) for k in STYLES}
            return
        single = CACHE_DIR / f"chime1_{int(vol * 100)}.wav"
        double = CACHE_DIR / f"chime2_{int(vol * 100)}.wav"
        if not single.exists():
            make_chime(single, [880], vol)
        if not double.exists():
            make_chime(double, [659.3, 987.8], vol)
        for k in STYLES:
            self.files[k] = str(single if k in ("follow", "redeem") else double)
        self.last = 0.0

    def play(self, kind):
        now = time.monotonic()
        if now - getattr(self, "last", 0.0) < 1.5:  # don't machine-gun during gift bombs / follow trains
            return
        self.last = now
        try:
            winsound.PlaySound(self.files.get(kind), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass


if __name__ == "__main__":
    login_window()
