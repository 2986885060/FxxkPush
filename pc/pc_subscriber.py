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
try:
    NTFY_TOKEN = os.environ.get("FP_NTFY_TOKEN") or (
        _SECRET.read_text().strip() if _SECRET.exists() else "")
except Exception:
    # 同 notification_listener：导入期读失败不该让 pythonw 无声退出。
    NTFY_TOKEN = ""
TOPICS = ["fp-vps", "fp-gray"]  # VPS hard alerts + gray-zone events

import pclog
LOG = pclog.get_logger("pc_subscriber")


def log(msg):
    pclog.log_auto(LOG, msg)


def _auth() -> dict:
    """鉴权头。**空 token 时不带头**，且每次取不到都会重试读文件。

    空 ``Bearer `` 是非法头值 → httpx 抛 LocalProtocolError（TransportError），
    在订阅热循环里每次重试都炸一遍（第 5 轮在 ai_triager 上确认的同型问题，
    这里的 stream 是一模一样的循环）。不带头则服务端回 401/403 —— 那是
    raise_for_status 能接住的正常异常，日志里也看得见。
    """
    global NTFY_TOKEN
    if not NTFY_TOKEN:
        try:
            NTFY_TOKEN = (os.environ.get("FP_NTFY_TOKEN")
                          or (_SECRET.read_text().strip()
                              if _SECRET.exists() else ""))
        except Exception:
            return {}
    return {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}

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
        pclog.append_rotating(
            ECHO, json.dumps({"ts": time.time(), "title": (title or "").strip(),
                              "msg": (message or "").strip()},
                             ensure_ascii=False),
            max_bytes=256 * 1024, mode="tail")
    except Exception as e:
        # 写失败 = 回环指纹缺失 -> notification_listener 认不出自己弹的 toast，
        # 窗口期内会把它当新通知重推给手机。静默吞掉等于把回环藏起来。
        log(f"record_echo failed (echo loop will misfire): {e!r}")


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
    with client.stream("GET", url, headers=_auth()) as resp:
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
                # read 不能是 None：和 ai_triager 第 4 轮那个 P1 是同一个坑 ——
                # 半开连接（休眠唤醒/NAT 超时/网络切换，没发 FIN 也没 RST）会让
                # iter_lines() 永久阻塞：无日志、不重连、watchdog 的 procs 检查
                # 还是绿的，fp-vps/fp-gray 的消息一直丢到重启为止。ntfy 实测
                # keepalive 45s 一次，120s 是它的 2.7 倍，不会误断健康连接。
                with httpx.Client(timeout=httpx.Timeout(connect=15, read=120,
                                                        write=15, pool=15),
                                  trust_env=False) as client:
                    for ev in stream(client, topic):
                        try:
                            handle(ev)
                        except Exception as e:
                            # 单条失败不断流：SSE 重连不回放，断开的 5 秒里
                            # 这个 topic 的消息会丢（与 ai_triager.consume 同构）。
                            log(f"[{topic}] handle failed ({e!r}), stream stays up")
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
                   headers=_auth(),
                   timeout=10, trust_env=False)
    print("self-test publish:", r.status_code)

if __name__ == "__main__":
    if "--test" in sys.argv:
        self_test()
    run()
