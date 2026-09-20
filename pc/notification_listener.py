#!/usr/bin/env python3
"""FuckPush Windows notification listener.

Polls UserNotificationListener for new toast notifications from IM apps
(QQ/微信/钉钉/TIM/学习通/企业微信...), dedupes by notification id, and:
  1. publishes each to ntfy topic fp-pc  (for AI triage later)
  2. archives locally to pc/notifications.jsonl (silent archive)

Runs as a long-lived process. New/unknown apps are captured too and
marked app_unknown=true so the AI layer can learn them.
"""
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from winrt.windows.ui.notifications import NotificationKinds
from winrt.windows.ui.notifications.management import (
    UserNotificationListener,
    UserNotificationListenerAccessStatus,
)

# ---------- config ----------
NTFY_BASE = os.environ.get("FP_NTFY_URL", "http://185.170.211.73:2586")
_SECRET = Path(__file__).parent / "ntfy.secret"
NTFY_TOKEN = os.environ.get("FP_NTFY_TOKEN") or (_SECRET.read_text().strip() if _SECRET.exists() else "")
TOPIC = "fp-pc"
POLL_SEC = 3
ARCHIVE = Path(__file__).parent / "notifications.jsonl"
STATE = Path(__file__).parent / "listener_state.json"

# apps worth watching; everything else is captured but flagged unknown
WATCHLIST = {"QQ", "微信", "WeChat", "钉钉", "DingTalk", "TIM", "学习通",
             "企业微信", "WeCom"}
# system noise to skip entirely
IGNORE = {"Windows.Defender.SecurityCenter", "无线", "AMD Software",
          "Microsoft Store", "Settings", "Windows 安全中心"}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def load_seen():
    if STATE.exists():
        try:
            return set(json.loads(STATE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen, cap=2000):
    if len(seen) > cap:
        seen = set(sorted(seen)[-cap:])
    STATE.write_text(json.dumps(sorted(seen)))


def extract(n) -> dict | None:
    """Parse one UserNotification into our event dict."""
    try:
        app = n.app_info.display_info.display_name
    except Exception:
        app = "?"
    if app in IGNORE:
        return None
    texts = []
    try:
        binding = n.notification.visual.get_binding("ToastGeneric")
        if binding:
            texts = [t.text for t in binding.get_text_elements() if t.text]
    except Exception:
        pass
    return {
        "id": str(n.id),
        "app": app,
        "texts": texts,
        "ts": time.time(),
        "watched": app in WATCHLIST,
    }


async def poll_once(listener, seen) -> list[dict]:
    events = []
    notifs = await listener.get_notifications_async(NotificationKinds.TOAST)
    for i in range(notifs.size):
        n = notifs.get_at(i)
        if str(n.id) in seen:
            continue
        ev = extract(n)
        if ev is None:
            seen.add(str(n.id))  # remember ignored ones too
            continue
        events.append(ev)
        seen.add(str(n.id))
    return events


def publish(ev: dict):
    """Send to ntfy (fp-pc) + local archive."""
    title = f"{ev['app']}: {ev['texts'][0][:60]}" if ev["texts"] else f"{ev['app']} 通知"
    body = "\n".join(ev["texts"]) or "<no text>"
    payload = {
        "topic": TOPIC,
        "title": title[:120],
        "message": body[:500],
        "priority": 3 if ev["watched"] else 1,
        "tags": ["bell"] if ev["watched"] else ["question"],
    }
    try:
        r = httpx.post(NTFY_BASE + "/", json=payload,
                       headers={"Authorization": f"Bearer {NTFY_TOKEN}"},
                       timeout=10)
        ok = r.status_code == 200
    except Exception as e:
        log(f"ntfy publish failed: {e!r}")
        ok = False
    ev["pushed"] = ok
    with ARCHIVE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    log(f"{'PUSH' if ok else 'ARCH'} [{ev['app']}] {' | '.join(ev['texts'])[:80]}")


async def main():
    listener = UserNotificationListener.current
    access = await listener.request_access_async()
    if access != UserNotificationListenerAccessStatus.ALLOWED:
        log("notification access DENIED — run probe_notifications.py first")
        return 1
    log(f"notification access OK, polling every {POLL_SEC}s -> topic {TOPIC}")

    seen = load_seen()
    log(f"loaded {len(seen)} known notification ids")

    # first pass marks existing notifications as seen (no flood on startup)
    for ev in await poll_once(listener, seen):
        pass  # discarded: pre-existing
    save_seen(seen)
    log("startup snapshot marked as seen; listening for NEW notifications")

    while True:
        try:
            events = await poll_once(listener, seen)
            for ev in events:
                publish(ev)
            if events:
                save_seen(seen)
        except Exception as e:
            log(f"poll error: {e!r}")
        await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
