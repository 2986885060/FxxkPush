#!/usr/bin/env python3
"""FuckPush PC subscriber — receive ntfy messages, pop Windows toasts.

Subscribes (SSE/JSON stream) to the VPS ntfy server and shows a toast
for every message. Console log mirrors everything.

Usage:
    python pc_subscriber.py            # foreground
    python pc_subscriber.py --test     # send self-test push then subscribe
"""
import json
import os
import sys
import time
import datetime
from pathlib import Path

import httpx

# ---------- config ----------
NTFY_BASE = os.environ.get("FP_NTFY_URL", "http://127.0.0.1:2586")  # via SSH tunnel (see pc/ntfy_tunnel.py)
_SECRET = Path(__file__).parent / "ntfy.secret"
NTFY_TOKEN = os.environ.get("FP_NTFY_TOKEN") or (_SECRET.read_text().strip() if _SECRET.exists() else "")
TOPICS = ["fp-vps", "fp-gray"]  # VPS hard alerts + gray-zone events

import pclog
LOG = pclog.get_logger("pc_subscriber")


def log(msg):
    pclog.log_auto(LOG, msg)

# ---------- toast ----------
_toaster = None

def get_toaster():
    global _toaster
    if _toaster is None:
        from winotify import Notification
        _toaster = Notification
    return _toaster

ECHO = Path(__file__).parent / "toast_echo.jsonl"


def record_echo(title, message):
    """Remember what we popped so notification_listener ignores the echo."""
    try:
        with ECHO.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "title": (title or "").strip(),
                                "msg": (message or "").strip()},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def toast(title, message, priority=3):
    record_echo(title, message)
    try:
        from winotify import Notification, audio
        n = Notification(app_id="FuckPush",
                         title=title or "FuckPush",
                         msg=message or "")
        if priority >= 4:
            n.set_audio(audio.LoopingAlarm, loop=False)
        elif priority == 3:
            n.set_audio(audio.Default, loop=False)
        else:
            n.set_audio(audio.Silent, loop=False)
        n.show()
    except Exception as e:
        log(f"toast failed ({e!r}), console only")

# ---------- subscribe ----------
def stream(client, topic):
    """Yield parsed events from one topic's JSON stream."""
    url = f"{NTFY_BASE}/{topic}/json"
    with client.stream("GET", url, headers={
            "Authorization": f"Bearer {NTFY_TOKEN}"}) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

def run():
    log(f"FuckPush PC subscriber starting, server={NTFY_BASE}, topics={TOPICS}")
    import threading
    stop = threading.Event()

    def worker(topic):
        while not stop.is_set():
            # Build a FRESH client per attempt, with trust_env=False.
            # httpx reads the Windows system proxy (registry ProxyEnable/
            # ProxyServer) when trust_env is on; a long-lived client keeps that
            # proxy baked into its mounts forever, so once v2rayN flips its
            # system proxy off the client hammers a dead port and every retry
            # fails with WinError 10061 until the process is restarted. Our
            # traffic is loopback -> SSH tunnel, so it must never use a proxy.
            try:
                with httpx.Client(timeout=httpx.Timeout(connect=15, read=None,
                                                        write=15, pool=15),
                                  trust_env=False) as client:
                    for ev in stream(client, topic):
                        handle(ev)
            except Exception as e:
                log(f"[{topic}] stream error: {e!r}, retry in 5s")
            stop.wait(5)

    try:
        threads = [threading.Thread(target=worker, args=(t,), daemon=True)
                   for t in TOPICS]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            time.sleep(5)
    except KeyboardInterrupt:
        log("bye")

def handle(ev):
    if ev.get("event") != "message":
        if ev.get("event") == "keepalive":
            return
        return
    # with 块：处理完自动把 trace 恢复，否则这条消息的 trace 会泄漏到
    # 下面 worker 循环的重连错误上
    with pclog.trace(pclog.extract_trace(ev)):
        title = ev.get("title") or ev.get("topic", "fuckpush")
        msg = ev.get("message", "")
        prio = ev.get("priority", 3)
        log(f"PUSH [{ev.get('topic')}] prio={prio} | {title}: {msg}")
        toast(title, msg, prio)

def self_test():
    """Send a test message through the server so we see it arrive."""
    payload = {"topic": "fp-vps", "title": "自测消息",
               "message": "PC subscriber 自测：看到这条说明链路通",
               "priority": 4, "tags": ["zap"]}
    r = httpx.post(NTFY_BASE + "/", json=payload,
                   headers={"Authorization": f"Bearer {NTFY_TOKEN}"},
                   timeout=10, trust_env=False)
    print("self-test publish:", r.status_code)

if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test()
    run()
