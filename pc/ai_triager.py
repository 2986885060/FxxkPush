#!/usr/bin/env python3
"""FuckPush AI triager — the brain.

Consumes two ntfy topics (fp-pc: Windows notifications, fp-gray: VPS
gray-zone events), asks MiMo to classify importance, and:
  - important  -> publish to fp-phone (phone buzzes)
  - unimportant-> silent archive

Also has dedup: same-kind events within a sliding window are batched and
only one push goes out with a count. Daily report at configured time.

Runs alongside pc_subscriber.py / notification_listener.py.
"""
import asyncio
import os
import json
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "triage_config.json").read_text(encoding="utf-8"))

NTFY_BASE = os.environ.get("FP_NTFY_URL", "http://127.0.0.1:2586")  # via SSH tunnel (see pc/ntfy_tunnel.py)
_SECRET = HERE / "ntfy.secret"
NTFY_TOKEN = (_SECRET.read_text().strip() if _SECRET.exists() else "")
ARCHIVE = HERE / "triage_log.jsonl"
STATE = HERE / "triage_state.json"

TOPICS_IN = {"fp-pc": "通知", "fp-gray": "VPS灰区", "fp-vps": "VPS硬规则"}
TOPIC_OUT = "fp-phone"

SYSTEM_PROMPT = """你是消息分诊助手。用户会在手机上收到你判定为"重要"的消息，\
所以只把真正需要立即知道的事情标为重要。

判定标准：
- 重要：涉及用户本人被点名/被找、日程与截止提醒、账户安全、服务异常需要处理、\
重复超过3次的同源事件（说明有人在坚持联系）、家人/紧急联系人消息
- 忽略：群聊闲聊、营销推广、验证码之外的系统通知、新闻推送、状态汇报

只输出 JSON，格式：{"label": "重要"|"忽略", "reason": "10字以内理由"}"""


import pclog
LOG = pclog.get_logger("ai_triager")


def log(msg):
    # file log: pythonw runs have no console, a crash must leave evidence.
    # pclog owns rotation/format/level — do not hand-roll it here.
    pclog.log_auto(LOG, msg)


def _log_crash(exc_type, exc, tb):
    import traceback
    log("FATAL " + "".join(traceback.format_exception(exc_type, exc, tb)).strip())
    os._exit(1)


sys.excepthook = _log_crash


def archive(rec):
    try:
        pclog.append_rotating(ARCHIVE, json.dumps(rec, ensure_ascii=False),
                              mode="rotate")
    except Exception as e:
        # 磁盘满/权限问题不该让一条消息掀翻消费循环
        log(f"archive write failed: {e!r}")


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception as e:
            # 状态损坏（断电/非原子写时代留下的半截 JSON）。load_state 在
            # main() 的 while True 之外，这里抛出去就是服务死、没人接得住。
            # 丢去重状态的最坏后果只是同一条消息重新分诊一次，比死强。
            log(f"triage state corrupt ({e!r}), resetting")
    return {"recent": {}, "last_report": ""}


def save_state(st):
    # 原子写：先落 .tmp 再 rename。直接 write_text 写一半断电会留下半截
    # JSON —— 这个函数在每条消息的 finally 里跑，非原子写等于埋雷。
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    tmp.replace(STATE)


async def classify(client: httpx.AsyncClient, kind: str, content: str) -> dict:
    payload = {
        "model": CONFIG["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"[{kind}] {content}"},
        ],
        "max_completion_tokens": CONFIG["max_tokens"],
        "thinking": {"type": "disabled"},
    }
    r = await client.post(f"{CONFIG['base_url'].rstrip('/')}/chat/completions",
                          json=payload,
                          headers={"Authorization": f"Bearer {CONFIG['api_key']}"},
                          timeout=30)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    # strip markdown fence if model added one
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def dedup_check(st, kind, content) -> str | None:
    """Collapse repeats of the same source within window. Returns summary
    override or None (first occurrence passes through)."""
    import re
    src = re.sub(r"\s+", "", content)[:40]
    now = time.time()
    win = CONFIG["dedup_window_sec"]
    st["recent"] = {k: v for k, v in st["recent"].items() if now - v["ts"] < win}
    key = f"{kind}:{src}"
    if key in st["recent"]:
        st["recent"][key]["count"] += 1
        return None  # suppressed, count recorded
    st["recent"][key] = {"ts": now, "count": 1}
    return "pass"


async def push_phone(client, title, message, priority=4):
    try:
        r = await client.post(NTFY_BASE + "/", timeout=10, json={
            "topic": TOPIC_OUT, "title": title, "message": message,
            "priority": priority,
            # tags carries the trace_id: ntfy drops every other custom field,
            # so this is the only channel that can carry the chain to the phone.
            "tags": pclog.tags_with_trace(["bell"]),
        }, headers={"Authorization": f"Bearer {NTFY_TOKEN}"})
        return r.status_code == 200
    except Exception as e:
        # 传输失败要在这里变成 False：冒出去会把整条 SSE 断掉（见 consume），
        # 而推送失败恰恰发生在网络抖动的时候 —— 断流等于雪上加霜。
        log(f"push_phone transport error: {e!r}")
        return False


async def handle_event(client, st, topic, ev):
    kind = TOPICS_IN.get(topic, topic)
    # ntfy carries everything as title+message text already
    title = (ev.get("title") or "").strip()
    body = (ev.get("message") or "").strip()
    content = f"{title}\n{body}".strip()
    if topic == "fp-pc":
        # title shape: "QQ: sender" -> app = QQ, rest = sender
        src = title.split(":")[0].strip() if ":" in title else "Windows通知"
        content = body or title
    else:
        src = topic

    # HARD RULE: @所有人 / @我 always push (bypasses AI + dedup). The vision
    # listener flags these too, but the triager is the gate to the phone — if
    # it does not enforce them here, its "群聊闲聊" verdict would silence a
    # message the user explicitly wants.
    if any(mark in content for mark in ("@所有人", "@我", "@全体成员")):
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "重要", "reason": "@提及硬规则"})
        ok = await push_phone(client, f"[提及] {src}", content[:200])
        log(f"MENTION_PUSH {'ok' if ok else 'FAIL'} [{src}] {content[:50]}")
        return

    # TEST RULE: anything containing "text" always goes straight to the
    # phone (bypasses AI + dedup) so omo can test the pipe anytime.
    if "text" in content.lower():
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "测试直推"})
        ok = await push_phone(client, f"[测试] {src}", content[:200])
        log(f"TEST_PUSH {'ok' if ok else 'FAIL'} [{src}] {content[:50]}")
        return

    # dedup first (no API call for repeats)
    if dedup_check(st, kind, content) is None:
        log(f"dedup suppressed [{src}] {content[:40]}")
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "重复抑制"})
        return

    try:
        verdict = await classify(client, kind, content)
    except Exception as e:
        log(f"classify failed ({e!r}) -> fallback: push (safer to buzz)")
        verdict = {"label": "重要", "reason": "AI异常默认推送"}

    label = verdict.get("label", "忽略")
    reason = verdict.get("reason", "")
    rec = {"ts": time.time(), "topic": topic, "src": src, "content": content,
           "label": label, "reason": reason}
    archive(rec)

    if label == "重要":
        # include repeat count if this source has history
        import re
        key = next((k for k in st["recent"]
                    if k.startswith(f"{kind}:") and
                    re.sub(r"\s+", "", content)[:40] in k), None)
        cnt = st["recent"].get(key, {}).get("count", 1) if key else 1
        suffix = f"（第{cnt}次）" if cnt > 1 else ""
        ok = await push_phone(client, f"{src}{suffix}", f"{content[:200]}\n— {reason}")
        log(f"{'PUSHED' if ok else 'PUSH_FAIL'} [{src}] {reason} | {content[:50]}")
    else:
        log(f"silent    [{src}] {reason} | {content[:50]}")


async def consume(client, st, topic):
    """JSON stream one topic, handle each message event."""
    url = f"{NTFY_BASE}/{topic}/json"
    async with client.stream("GET", url, headers={
            "Authorization": f"Bearer {NTFY_TOKEN}"}) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") != "message":
                continue
            # trace_id starts here for this message: ntfy only forwards tags,
            # so the id rides in tags (or falls back to the ntfy message id,
            # which is identical for every subscriber of the same message).
            # with 块结束自动恢复上下文。直接 bind_from_event() 是不恢复的：
            # main() 里那句 "stream error ... reconnect in 5s" 会挂上最后一条
            # 消息的 trace，grep 那条消息时会捞到一堆无关的重连错误
            # （pc_subscriber 已因同一个 bug 修过，这里是同一处漏网）。
            with pclog.trace(pclog.extract_trace(ev)):
                # fp-pc arrives as JSON string inside message (from listener);
                # fp-gray is our gray JSON; fp-vps is plain text
                body = ev.get("message", "")
                parsed = None
                try:
                    maybe = json.loads(body)
                    if isinstance(maybe, dict):
                        parsed = maybe.get("data", maybe)
                        if isinstance(parsed, str):
                            parsed = {"title": ev.get("title", ""), "message": body}
                        else:
                            parsed = {"title": ev.get("title", ""), **parsed}
                except json.JSONDecodeError:
                    parsed = {"title": ev.get("title", ""), "message": body}
                # 结构性兜底：单条消息失败绝不能断开 SSE —— 重连不回放，
                # 断开的这几秒里该 topic 的消息会永久丢失。classify/push_phone
                # 各自有 try，这里防的是 archive、未来的调用、以及任何没想到的
                # 异常。save_state 放 finally：失败那条也得把去重状态存下去，
                # 否则同一条消息重连后会再分诊一次。
                try:
                    await handle_event(client, st, topic, parsed)
                except Exception as e:
                    log(f"handle_event failed ({e!r}), stream stays up")
                finally:
                    save_state(st)


async def main():
    log(f"triager up: model={CONFIG['model']}, out={TOPIC_OUT}")
    st = load_state()
    timeout = httpx.Timeout(connect=15, read=None, write=15, pool=15)
    while True:
        # Rebuild the client on every reconnect attempt with trust_env=False.
        # With trust_env on, httpx snapshots the Windows system proxy at
        # construction time; the long-lived client then keeps retrying through
        # a proxy port that v2rayN may have since torn down, and every request
        # fails (WinError 10061) until the process restarts. We talk to a
        # loopback tunnel and to the API directly, so never use a proxy.
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                # one task per input topic
                await asyncio.gather(
                    *(consume(client, st, t) for t in TOPICS_IN)
                )
        except Exception as e:
            log(f"stream error: {e!r}, reconnect in 5s")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
