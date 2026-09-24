"""Scripted chat for README screenshots: fake viewers, real global emotes, every alert type.

    python overlay.py --demo            (add --edit to start in edit mode)

No Twitch connection, hotkeys still work, nothing is saved to config.json.
"""

import time

# Twitch global emotes by id (they arrive with positions in the IRC "emotes" tag)
TWITCH = {"Kappa": "25", "LUL": "425618", "<3": "555555584"}

SCRIPT = [
    # (delay, kind, payload)
    (0.2, "chat", ("PixelPanda", "#1E90FF", "yo whats up chat")),
    (0.3, "chat", ("grubhunter99", "#FF69B4", "first time catching the stream live peepoHappy")),
    (0.3, "alert", {"kind": "follow", "text": "cozy_quokka followed", "message": ""}),
    (0.3, "chat", ("NightOwlNils", "#9ACD32", "that jump was insane Clap")),
    (0.3, "chat", ("MossyBoots", "#DAA520", "LUL LUL")),
    (0.3, "alert", {"kind": "sub", "text": "MossyBoots subscribed at Tier 1. They've subscribed for 6 months!",
                    "message": "half a year already peepoHappy"}),
    (0.3, "chat", ("ferret_fan", "#00FF7F", "how many deaths are we at now")),
    (0.3, "chat", ("PixelPanda", "#1E90FF", "dont ask Kappa")),
    (0.3, "alert", {"kind": "raid", "text": "14 raiders from SnowyStreams have joined!", "message": ""}),
    (0.3, "chat", ("SnowyStreams", "#8A2BE2", "hiii everyone AlienDance PepePls")),
    (0.3, "alert", {"kind": "bits", "text": "grubhunter99 cheered 100 bits", "message": "for the climb EZ"}),
    (0.3, "chat", ("NightOwlNils", "#9ACD32", "gg that was clean FeelsStrongMan")),
]

ALL_ALERTS = [  # used for the alerts close-up
    {"kind": "follow", "text": "cozy_quokka followed", "message": ""},
    {"kind": "sub", "text": "MossyBoots subscribed at Tier 1. They've subscribed for 6 months!", "message": ""},
    {"kind": "gift", "text": "PixelPanda is gifting 5 Tier 1 Subs to the community!", "message": ""},
    {"kind": "raid", "text": "14 raiders from SnowyStreams have joined!", "message": ""},
    {"kind": "bits", "text": "grubhunter99 cheered 100 bits", "message": ""},
    {"kind": "redeem", "text": "ferret_fan redeemed Hydrate!", "message": ""},
]


def fake_privmsg(name, color, text):
    """Build the same dict parse_irc() returns, with Twitch emote positions filled in."""
    ranges, pos = {}, 0
    for word in text.split(" "):
        if word in TWITCH:
            ranges.setdefault(TWITCH[word], []).append(f"{pos}-{pos + len(word) - 1}")
        pos += len(word) + 1
    tags = {"display-name": name, "color": color, "id": f"demo-{time.monotonic_ns()}",
            "emotes": "/".join(f"{eid}:{','.join(r)}" for eid, r in ranges.items())}
    login = name.lower()
    return {"tags": tags, "prefix": f"{login}!{login}@{login}.tmi.twitch.tv", "cmd": "PRIVMSG",
            "params": ["#demo"], "trailing": text}


def demo_thread(events, only_alerts=False):
    events.put(("status", "demo mode (no Twitch connection)"))
    events.put(("alert_status", "alerts: scripted demo"))
    events.put(("room", "0"))  # no such channel: loads the global 7TV/BTTV emote sets only
    time.sleep(2.5)            # let the emote lists arrive before the first message
    if only_alerts:
        for a in ALL_ALERTS:
            events.put(("alert", a))
            time.sleep(0.15)
        return
    for delay, kind, payload in SCRIPT:
        time.sleep(delay)
        events.put(("chat", fake_privmsg(*payload)) if kind == "chat" else ("alert", payload))

