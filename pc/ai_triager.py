#!/usr/bin/env python3
"""FuckPush AI triager — the brain.

Consumes four ntfy topics (fp-pc: Windows notifications, fp-gray: VPS
gray-zone events, fp-vps: VPS hard alerts, fp-feedback: the 👍/👎 buttons
on pushes this service sent), asks MiMo to classify importance, and:
  - important  -> publish to fp-phone (phone buzzes)
  - unimportant-> silent archive

Repeats of the same source+content inside a sliding window are suppressed
entirely (no API call, no push). Daily report at configured time.

Runs alongside pc_subscriber.py / notification_listener.py.
"""
import asyncio
import os
import json
import re
import sys
import time
from pathlib import Path

import httpx

import pclog
import fp_feedback

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "triage_config.json").read_text(encoding="utf-8"))

NTFY_BASE = os.environ.get("FP_NTFY_URL", "http://127.0.0.1:2586")  # via SSH tunnel (see pc/ntfy_tunnel.py)
_SECRET = HERE / "ntfy.secret"
NTFY_TOKEN = (_SECRET.read_text().strip() if _SECRET.exists() else "")
ARCHIVE = HERE / "triage_log.jsonl"
STATE = HERE / "triage_state.json"

TOPICS_IN = {"fp-pc": "通知", "fp-gray": "VPS灰区", "fp-vps": "VPS硬规则"}
# 反馈 topic 不进 TOPICS_IN：那个 dict 的 value 是给 AI 的 kind 标签，
# 反馈要是混进去就会被当成 [fp-feedback] 待分诊消息问一遍模型。
ALL_TOPICS = [*TOPICS_IN, fp_feedback.TOPIC]
TOPIC_OUT = "fp-phone"

SYSTEM_PROMPT = """你是消息分诊助手。用户会在手机上收到你判定为"重要"的消息，\
所以只把真正需要立即知道的事情标为重要。

判定标准：
- 重要：涉及用户本人被点名/被找、日程与截止提醒、账户安全、服务异常需要处理、\
重复超过3次的同源事件（说明有人在坚持联系）、家人/紧急联系人消息
- 忽略：群聊闲聊、营销推广、验证码之外的系统通知、新闻推送、状态汇报

只输出 JSON，格式：{"label": "重要"|"忽略", "reason": "10字以内理由"}"""


LOG = pclog.get_logger("ai_triager")

# 推送快照的存活期。超过一周的👍/👎已经没有纠正意义，占着 state 只会让
# 每次 save_state 都多写一堆死数据 —— 反馈是低频事件，7 天足够。
PUSH_TTL = 7 * 24 * 3600


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


def _prune_pushes(st: dict) -> None:
    """丢掉过期的推送快照。启动时和每次入队后各跑一次 —— 环形缓冲的最小
    实现，不用后台定时器（多一个任务就多一个可能挂在 finally 外的异常）。"""
    pushes = st.get("pushes")
    if not isinstance(pushes, dict) or not pushes:
        st["pushes"] = {}
        return
    now = time.time()
    st["pushes"] = {k: v for k, v in pushes.items()
                    if isinstance(v, dict) and now - v.get("ts", 0) < PUSH_TTL}


def load_state():
    st = None
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception as e:
            # 状态损坏（断电/非原子写时代留下的半截 JSON）。load_state 在
            # main() 的 while True 之外，这里抛出去就是服务死、没人接得住。
            # 丢去重状态的最坏后果只是同一条消息重新分诊一次，比死强。
            log(f"triage state corrupt ({e!r}), resetting")
    if not isinstance(st, dict):
        st = {"recent": {}, "last_report": ""}
    if not isinstance(st.get("recent"), dict):
        # 「可解析但形状不对」：recent 要是字符串/列表，setdefault 只补键不
        # 校验类型，dedup_check 的 .items() 会让除硬规则外的每条消息都抛，
        # 被 consume 吞掉 —— 服务看着活着，AI 路径其实全废。
        st["recent"] = {}
    if not isinstance(st.get("pushes"), dict):
        st["pushes"] = {}
    if not isinstance(st.get("last_report"), str):
        st["last_report"] = ""
    _prune_pushes(st)
    return st


def save_state(st):
    # 原子写：先落 .tmp 再 rename。直接 write_text 写一半断电会留下半截
    # JSON —— 这个函数在每条消息的 finally 里跑，非原子写等于埋雷。
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
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


async def push_phone(client, title, message, priority=4, actions=None):
    try:
        payload = {
            "topic": TOPIC_OUT, "title": title, "message": message,
            "priority": priority,
            # tags carries the trace_id: ntfy drops every other custom field,
            # so this is the only channel that can carry the chain to the phone.
            "tags": pclog.tags_with_trace(["bell"]),
        }
        if actions:
            payload["actions"] = actions
        r = await client.post(NTFY_BASE + "/", timeout=10, json=payload,
                              headers={"Authorization": f"Bearer {NTFY_TOKEN}"})
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

    # 源级规则快速路：这个源被人工点过 3 次👎 → 直接静默归档，连 AI 都不问
    # （省一次 API 调用）。排在两条硬规则之后：@提及和测试通道是用户明确要
    # 的东西，不该被一条自动学来的规则压掉。
    # 只对 fp-pc 生效：其它 topic 的 src 恒等于 topic 名（见上面的 else 分支），
    # 对 fp-vps 点 3 次👎 会静音掉整条服务器告警通道 —— 磁盘爆了手机不响，
    # 这个口子绝不能开。按钮侧也做了同样的限定（push 段），这里是第二道闸。
    if topic == "fp-pc" and fp_feedback.should_ignore(src):
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "规则静默",
                 "reason": f"源[{src}]误判累积"})
        log(f"rule-silenced [{src}] {content[:50]}")
        return

    try:
        verdict = await classify(client, kind, content)
        if not isinstance(verdict, dict):
            # 模型完全可能返回合法 JSON 但不是对象（["重要"] / "重要" /
            # null —— classify 自己就在处理模型不守 fence，说明这类偏移真实
            # 存在）。类型检查必须放在 try **里面**：放外面的话这里抛
            # AttributeError，本该兜底的「AI 异常默认推送」反而失效，
            # 消息被 consume 吞掉（第 2 轮 P2）。
            raise TypeError(f"model returned {type(verdict).__name__}: "
                            f"{str(verdict)[:60]}")
    except Exception as e:
        log(f"classify failed ({e!r}) -> fallback: push (safer to buzz)")
        verdict = {"label": "重要", "reason": "AI异常默认推送"}

    label = verdict.get("label", "忽略")
    reason = verdict.get("reason", "")
    rec = {"ts": time.time(), "topic": topic, "src": src, "content": content,
           "label": label, "reason": reason}
    archive(rec)

    if label == "重要":
        # 推送快照：反馈回来时靠它还原 src/content。triage_log 是轮转的，
        # 几天后点一次按钮去那里查只会读到空 —— 所以自包含地存一份。
        # 只有 fp-pc 才带按钮：其它 topic 的 src 就是 topic 名本身，规则粒度
        # 是「整条通道」，3 次👎 会把 VPS 告警整条静音（不可接受）。
        #
        # 这里原本还有「（第N次）」后缀：dedup_check 对窗口内的重复直接
        # return None（静默抑制，不推也不调 AI），所以能走到推送的必然是首次，
        # count 恒为 1、后缀恒为空 —— 死代码。而且它用子串匹配找 key，
        # content 去空白后为空串时 `"" in k` 恒真，会把别人的计数算到自己头上。
        # 一并删掉（第 2 轮 P2 + 性能项）。
        pid = pclog.new_trace_id()
        actions = fp_feedback.build_actions(pid) if topic == "fp-pc" else None
        if actions:
            # 没有 actions 就不会有反馈，快照存着只会在 state 里躺满 7 天。
            # content 截到 500：够做微调语料，又不至于让 save_state 每条消息
            # 都重写几万字节（推送本身也只发前 200 字）。
            st["pushes"][pid] = {
                "ts": time.time(), "topic": topic, "src": src,
                "content": content[:500], "label": label, "reason": reason,
            }
            _prune_pushes(st)
        ok = await push_phone(client, src, f"{content[:200]}\n— {reason}",
                              actions=actions)
        if not ok and actions:
            # 没推出去 = 手机上不会出现按钮 = 这个 pid 永远等不到反馈，
            # 留着只是白占 7 天 state
            st["pushes"].pop(pid, None)
        log(f"{'PUSHED' if ok else 'PUSH_FAIL'} [{src}] {reason} "
            f"push_id={pid} {'+fb' if actions else ''} | {content[:50]}")
    else:
        log(f"silent    [{src}] {reason} | {content[:50]}")


def _unwrap(ev: dict, body: str) -> dict:
    """把裹在 message 字符串里的负载还原成 dict。

    fp-pc 是 JSON、fp-gray 是 JSON、fp-vps 是纯文本，三种形状在这里统一成
    {"title": ..., ...}。解析失败一律退回 {"title", "message"} 兜底，绝不抛。

    顺带修掉一个边界：body 是合法 JSON 但不是对象（`null` / `123` / `"x"`）时，
    原实现在 isinstance 检查失败后会让 parsed 保持 None，一路传到
    handle_event 的 ev.get("title") 上炸 AttributeError。
    """
    try:
        maybe = json.loads(body)
    except json.JSONDecodeError:
        maybe = None
    if isinstance(maybe, dict):
        data = maybe.get("data")
        if isinstance(data, dict):
            # 顶层键和 data 里的键**都要**：handle_event 拿 kind 当事件类型
            # 标签（dedup 也用它分组），原来只 `{"title", **data}` 会把顶层的
            # kind/ts 一起丢掉 —— fp-gray 的形状正是 {"ts","kind","data"}。
            # message 兜底必须铺在最前，否则 fp-gray 的 data 里没有 message 键
            # 时 handle_event 会拿到空 body，AI 分诊只剩个标签、内容全丢
            # （第 2 轮 P2，既有缺陷，借这次抽出一并修）。
            # 顶层若带 message/title 则照旧覆盖，与 fp-pc 原行为一致。
            return {"title": ev.get("title", ""), "message": body,
                    **maybe, **data}
        # data 不是对象（str/None/int/list）→ 顶层原样返回
        return {"title": ev.get("title", ""), "message": body, **maybe}
    # 走到这里：不是 JSON，或 JSON 不是对象（["重要"] / null / 数字）。
    # 原实现在 isinstance(parsed, str) 为假时直接 **parsed —— **None 会
    # TypeError，违背自己「绝不抛」的 docstring，消息被 consume 吞掉。
    return {"title": ev.get("title", ""), "message": body}


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
                # 结构性兜底：单条消息失败绝不能断开 SSE —— 重连不回放，
                # 断开的这几秒里该 topic 的消息会永久丢失。classify/push_phone
                # 各自有 try，这里防的是 archive、未来的调用、以及任何没想到的
                # 异常。save_state 放 finally：失败那条也得把去重状态存下去，
                # 否则同一条消息重连后会再分诊一次。
                try:
                    if topic == fp_feedback.TOPIC:
                        # 反馈分支不走 _unwrap：那层解析会把 {"verdict": ...}
                        # 摊平、丢掉 message 键，handle 再 json.loads 一次就是
                        # 白做功 + 拿不到东西（两边注释都记了这个坑）。
                        fp_feedback.handle(st, body)
                    else:
                        await handle_event(client, st, topic, _unwrap(ev, body))
                except Exception as e:
                    log(f"handle failed ({e!r}), stream stays up")
                finally:
                    save_state(st)


async def supervise(client, st, topic, max_fails: int = 12):
    """单条流的监督者：这条流挂了只重连它自己。

    原来是 ``gather(*(consume(...)))``，而 asyncio.gather 默认**第一个异常
    就向上传播并 cancel 其余任务**。fp-feedback 是全新链路（发布端实测过
    不等于订阅端没问题），它一旦 401/404，另外三条主流会被一个自己用不到
    的 topic 每 5 秒拖断一次 —— 而重连不回放，断开那几秒的消息永久丢失。

    连续失败攒到 max_fails（12 × 5s = 1 分钟）就往外抛，让 main 重建
    httpx client：client 级的故障（系统代理毒化那种）在流内小打小闹治不好，
    必须换掉整个客户端 —— 原 main 的重连逻辑正是为此存在，不能丢。
    """
    fails = 0
    config_retries = 0
    while True:
        try:
            await consume(client, st, topic)
            fails = 0  # 服务端主动断开也算一次干净收尾
        except Exception as e:
            fails += 1
            log(f"[{topic}] stream error: {e!r} #{fails}, reconnect in 5s")
            if fails >= max_fails:
                fails = 0
                status = getattr(getattr(e, "response", None),
                                 "status_code", None)
                if status in (401, 403, 404, 410):
                    # 配置级故障：换 client 一点用没有，可要是照旧 raise，
                    # 4 条流会被这个自己用不到的 topic 每 ~65 秒整体拖断一次，
                    # 而重连不回放 —— 断开那几秒的消息永久丢失（第 2 轮 P2）。
                    # 改成让**这条流**自己指数退避，其余三条照常跑；不设上限，
                    # 配置改好后的下一轮重试自然恢复，不做「永久放弃」——
                    # 隧道断 80 分钟那种长故障不该把 topic 判死。
                    config_retries += 1
                    delay = min(600, 30 * 2 ** min(config_retries, 5))
                    log(f"[{topic}] HTTP {status} 配置级故障：单流退避 {delay}s"
                        f"（其余流不受影响，第 {config_retries} 次）")
                    await asyncio.sleep(delay)
                    continue
                raise   # 传输级故障才升级：那才是换 client 能治的
        await asyncio.sleep(5)   # 正常收尾也要等：否则服务端秒断会变成紧循环


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
                # 每条流在 supervise 里自己重连；这一层只处理 client 级故障
                await asyncio.gather(
                    *(supervise(client, st, t) for t in ALL_TOPICS)
                )
        except Exception as e:
            log(f"client error: {e!r}, rebuilding client in 5s")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
