"""Where the overlay keeps its files, and the .env (account/connection settings).

.env        channel + Twitch app credentials (what you'd change per streamer)
config.json look & behaviour (position, fonts, fade times, alert toggles...)
.tokens.json  Twitch login for follow/redemption alerts (created by the login window)
cache/      downloaded emotes and generated chime sounds
"""

import sys
from pathlib import Path

# next to the .exe when packaged with PyInstaller, next to the scripts otherwise
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # read-only files shipped inside the exe
ENV_PATH = APP_DIR / ".env"
CONFIG_PATH = APP_DIR / "config.json"
TOKEN_PATH = APP_DIR / ".tokens.json"
CACHE_DIR = APP_DIR / "cache"

# Public Twitch app shipped with the overlay (Device Code Flow, no secret). Public
# client IDs are not secret - every login still has to be approved by the streamer.
DEFAULT_CLIENT_ID = "3zwiy9oad24p01x3r81mha9fthdiko"

ENV_TEMPLATE = """\
# Twitch Chat Overlay - account settings. Lines starting with # are ignored.

# The channel whose chat you want on screen (your Twitch username).
TWITCH_CHANNEL={channel}

# Optional. Follow + channel point alerts use the overlay's built-in Twitch app,
# so you only need to run "Login to Twitch" once. Leave this empty unless you
# want to use your own app from https://dev.twitch.tv/console/apps
TWITCH_CLIENT_ID={client_id}

# Only for your own *Confidential* app. Leave empty otherwise.
TWITCH_CLIENT_SECRET={client_secret}
"""


def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(updates: dict):
    """Update keys in place (keeping comments); create the file from the template if missing."""
    if not ENV_PATH.exists():
        values = {"channel": "", "client_id": "", "client_secret": ""}
        ENV_PATH.write_text(ENV_TEMPLATE.format(**values), encoding="utf-8")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    pending = dict(updates)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
