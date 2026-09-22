#!/usr/bin/env python3
"""FxxkPush 误判反馈闭环 —— 手指点一次按钮，规则学一次。

链路（每一环都实测过，不是照文档抄的）：

    ai_triager 判「重要」→ 推 fp-phone 时附带 👍/👎 按钮（ntfy http action）
      → 手机点按钮 → ntfy app 发 POST 到 VPS 的 /fp-feedback
      → 本模块 consume → feedback.jsonl 落地（带 content 快照 = 微调语料）
      → 同一个源连续 N 次 👎 → 写进规则 → 之后该源直接静默、跳过 AI

设计要点，全是踩出来的：

1. 反馈只认 ``message`` 字段。实测 ntfy 会把 action 的 ``body_type`` 丢掉，
   手机实际发出的 Content-Type 因此不可知；而 ``url`` 里带上 topic 时，
   json 和 text/plain 两种发法都返回 200、message 都是原样 JSON。
   同时实测反馈消息的 ``title`` 和 ``tags`` 都是 None —— 任何依赖它们的
   匹配都会静默失配，所以 push_id 只能寄生在 body 里。

2. 反馈记录自带 content 快照，不回头去查 triage_log.jsonl。那个文件是轮转的，
   几天后点一次👍会读到空；而且语料本来就该自包含。

3. 规则只作用于「源」（QQ / 企业微信 / Hermes ...），不作用于单条消息。
   单条的纠正改不动模型，源级的纠正能直接省掉一次 API 调用。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pclog

HERE = Path(__file__).parent
LOG = pclog.get_logger("fp_feedback")

TOPIC = "fp-feedback"
FEEDBACK_FILE = HERE / "feedback.jsonl"
RULES_FILE = HERE / "triage_rules.json"

# 模块级缓存：地址和 token 都是启动期就定死的，每条重要推送重读一次文件
# 属于白白多出来的 IO（同 rules 的 mtime 缓存一个道理）。定义必须在
# public_base/_token 之前 —— 否则 Pyright 报 unbound。
_base_cache: str | None = None
_token_cache: str | None = None

# 连续几次「不该推」就闭嘴。3 次是折中：一次手滑不该改规则，
# 三次还点👎说明这个源确实不该吵他。
BAD_STREAK = 3
# 规则文件的内存缓存：反馈是低频事件，但 should_ignore() 每条消息都会问，
# 每次都 open+json.loads 等于给主链路加一次磁盘读。
_rules_cache: dict | None = None
_rules_mtime: float = -1.0


def log(msg: str) -> None:
    pclog.log_auto(LOG, msg)


# ---------------------------------------------------------------- 通道地址
def public_base() -> str:
    """手机点按钮时要打的地址 —— 必须是 VPS 公网口，不是 127.0.0.1。

    手机不在这个网络里，本地隧道它够不着（README「手机」一节：订阅地址填
    http://VPS-IP:2586）。优先取环境变量，否则从 vps.secret 第一段推导，
    与 ntfy_tunnel.py 同一份凭据，不新增配置项。
    """
    env = os.environ.get("FP_NTFY_PUBLIC", "").strip()
    if env:
        return env.rstrip("/")
    global _base_cache          # 不写这行，下面的赋值会让它变成本函数的局部变量，
    if _base_cache:             # 于是这句读到未赋值的局部 → UnboundLocalError
        return _base_cache
    try:
        secret = HERE.parent / "vps.secret"
        host = secret.read_text(encoding="utf-8").split()[0]
        # 成功才缓存：失败要保留每次重试的机会（以及每次那行告警日志）
        _base_cache = f"http://{host}:2586"
        return _base_cache
    except Exception as e:
        # 推不出地址时按钮点了必然 404，宁可不放按钮也不能放个坏按钮
        log(f"public_base unavailable ({e!r}), actions disabled")
        return ""


def _token() -> str:
    """读一次就缓存。每条重要推送都要调 build_actions，每轮读 2 个小文件
    虽然只有几十次/天，但和同模块为规则做 mtime 缓存的动机必须一致。

    **失败不缓存**：AV/权限抖动让首次读失败就永久缓存成空串的话，本进程
    之后所有推送永远没按钮、还没一行日志 —— 和 public_base() 里写明的取舍
    （成功才缓存、失败留重试和告警）矛盾。宁可每条多读一次小文件。
    测试里直接替换 _token 这个函数对象即可绕过缓存。"""
    global _token_cache
    if _token_cache is None:
        try:
            _token_cache = (HERE / "ntfy.secret").read_text(encoding="utf-8").strip()
        except Exception as e:
            log(f"ntfy.secret unreadable ({e!r}) —— 本轮推送不带反馈按钮")
            return ""
    return _token_cache


# ---------------------------------------------------------------- 按钮构造
def build_actions(push_id: str) -> list[dict] | None:
    """👍/👎 两个 ntfy http action。返回 None 表示通道不可用（不放按钮）。

    url 带 topic（``/fp-feedback``），body 是纯 JSON —— 这样手机无论用哪种
    Content-Type 发都能进得来（见模块 docstring 第 1 条）。
    """
    base = public_base()
    tok = _token()
    if not base or not tok or not push_id:
        return None
    actions = []
    for verdict, label, emoji in (("good", "准", "thumbs_up"),
                                  ("bad", "误判", "thumbs_down")):
        body = json.dumps({"verdict": verdict, "push_id": push_id},
                          ensure_ascii=False)
        actions.append({
            "action": "http",
            "label": label,
            "tag": emoji,
            "clear": True,          # 点完就收走，别留个已处理的按钮在通知栏
            "url": f"{base}/{TOPIC}",
            "method": "POST",
            "headers": {"Authorization": f"Bearer {tok}",
                        "Content-Type": "application/json"},
            "body": body,
        })
    return actions


# ---------------------------------------------------------------- 规则
def _load_rules() -> dict:
    """带 mtime 缓存地读规则。缓存失效由写入方自己触发。

    写方有两个（常驻服务 + 命令行 CLI），所以**不是**单进程：
    读-改-写的并发最多互相覆盖一次反馈计数（可接受），临时名带 pid 防的
    是「交错出半截 JSON」；文件共享层面的替换冲突见 _save_rules。"""
    global _rules_cache, _rules_mtime
    try:
        mtime = RULES_FILE.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _rules_cache is None or mtime != _rules_mtime:
        if mtime < 0:
            fresh: dict = {"sources": {}}
        else:
            try:
                loaded = json.loads(RULES_FILE.read_text(encoding="utf-8"))
                fresh = loaded if isinstance(loaded, dict) else {"sources": {}}
            except Exception as e:
                # 规则文件坏了最坏后果是回到「每条都问 AI」，不是停摆，
                # 所以重置而不是崩 —— 和 triage_state 同样的取舍。
                log(f"rules corrupt ({e!r}), resetting")
                fresh = {"sources": {}}
        # 「可解析但形状不对」也是损坏的一种：sources 要是字符串/列表，
        # 后面 should_ignore 的 .get(src) 和 _apply_rule 的 setdefault 都会
        # AttributeError，而那是在主链路里被调用的 —— 解析失败挡不住这一类。
        if not isinstance(fresh.get("sources"), dict):
            fresh["sources"] = {}
        else:
            # 单条 entry 也可能是 "QQ": "ignore" 这种形状。第 1 轮只给写端
            # (_apply_rule)加了校验，读端(should_ignore / CLI)漏了 —— 在这里
            # 一次性洗掉，读写两边就都不用各自再防一遍。
            fresh["sources"] = {k: v for k, v in fresh["sources"].items()
                                if isinstance(v, dict)}
        _rules_cache = fresh
        _rules_mtime = mtime
    # fresh/_rules_cache 在所有分支都已赋值，类型收窄靠这个断言之后的局部量
    rules = _rules_cache
    return rules if rules is not None else {"sources": {}}


def _save_rules(rules: dict) -> None:
    """原子写 + 主动刷新缓存。先落 .tmp 再 rename，写一半断电不会留半个 JSON。

    临时名必须带 pid：现在有两个写者（常驻服务 + 命令行 CLI），共用一个固定
    的 ``.tmp`` 会在两边同时写时交错出半截 JSON —— 那会被下次加载当成「损坏」
    整个重置，学到的规则一条不剩。唯一临时名消掉交错；至于读-改-写互相覆盖
    最多丢一次反馈计数，代价可接受，不值得为此引入文件锁。
    """
    global _rules_cache, _rules_mtime
    rules["updated"] = time.time()
    tmp = RULES_FILE.with_name(f"{RULES_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rules, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    try:
        tmp.replace(RULES_FILE)
    except PermissionError:
        # Windows 上读端（另一进程正 read_text 持着句柄）的瞬间 rename 会撞
        # PermissionError。先清掉临时文件再抛，否则每失败一次留一个
        # .<pid>.tmp 孤儿。上层：CLI 看到 traceback，服务侧被 handle 的 try
        # 吃掉并记日志 —— 都比静默失败好。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        log(f"rules replace failed (reader holds {RULES_FILE.name}) —— "
            "本次规则更新未落盘，下次反馈重试")
        raise
    _rules_cache = rules
    try:
        _rules_mtime = RULES_FILE.stat().st_mtime
    except OSError:
        _rules_mtime = -1.0


def should_ignore(src: str) -> bool:
    """主链路的快速路：True = 这个源已判定为噪音，连 AI 都不用问。

    isinstance 校验是给 _load_rules 之外的路径兜底的纵深防御：规则文件里
    要是出现 ``"QQ": "ignore"`` 这种「值不是对象」的形状，读端直接 .get()
    会 AttributeError —— 而这条调用在 classify 的 try 之外，冒出去会被
    consume 吞掉，那条消息连归档都不归档，比「回到每条问 AI」更糟。
    """
    if not src:
        return False
    entry = _load_rules().get("sources", {}).get(src)
    return isinstance(entry, dict) and entry.get("verdict") == "ignore"


def _apply_rule(src: str, verdict: str) -> None:
    """把一次人肉反馈折算成源级规则。"""
    rules = _load_rules()
    entry = rules.setdefault("sources", {}).setdefault(
        src, {"bad": 0, "good": 0, "verdict": None})
    # entry 必是 dict：_load_rules 已把 sources 清成「值全是对象」，setdefault
    # 的默认值也是 dict，两条入口都保证了（第 1 轮加的 isinstance 分支因此
    # 不可达，第 3 轮删掉）。bad 不是数字仍可能（手改文件），下面继续防。
    try:
        prev_bad = int(entry.get("bad", 0) or 0)
        prev_good = int(entry.get("good", 0) or 0)
    except (TypeError, ValueError):
        prev_bad = prev_good = 0

    if verdict == "bad":
        entry["bad"] = prev_bad + 1
    else:
        # 一条👍抵掉一次👎：他刚说这条推得准，说明该源还没到该闭嘴的程度
        entry["good"] = prev_good + 1
        entry["bad"] = max(0, prev_bad - 1)
    entry["last"] = time.time()

    if verdict == "bad" and entry["bad"] >= BAD_STREAK \
            and entry.get("verdict") != "ignore":
        entry["verdict"] = "ignore"
        entry["since"] = time.time()
        log(f"RULE [{src}] 连续 {entry['bad']} 次「误判」→ 转入静默"
            f"（跳过 AI，只归档）")
    elif verdict == "good" and entry.get("verdict") == "ignore":
        # 放出来的唯一路径：他主动对一个已被拉黑的源点了👍 —— 规则错了，
        # 立刻撤销，否则拉黑是单向门，误伤一次就永远收不到这个源了。
        # 计 prev_bad 而不是读 entry['bad']：good 分支刚刚把它减过 1。
        # manual 的静音不走这条路：那是人工刻意压掉的，只有 CLI unmute 能解，
        # 否则一条几天前的旧通知就够把它放出来。
        if not entry.get("manual"):
            entry["verdict"] = None
            entry["bad"] = 0
            log(f"RULE [{src}] 收到👍，撤销静默（撤销前误判计数 {prev_bad}）")
    _save_rules(rules)


# ---------------------------------------------------------------- 消费
def _archive(rec: dict) -> None:
    try:
        pclog.append_rotating(FEEDBACK_FILE,
                              json.dumps(rec, ensure_ascii=False),
                              mode="rotate")
    except Exception as e:
        # 写不进 = 这次人肉标注白点了。必须吵出来，不能当没发生 ——
        # 反馈本来就是低频事件，丢一条就少一条语料。
        log(f"feedback archive failed: {e!r}")


def handle(st: dict, body: str) -> None:
    """consume 的分流目标。全程不得抛：feedback 断了不能带塌分诊流。

    同步而非 async —— 这里只有两次小文件写（save_state 在调用方的 finally
    里，规则文件几 KB），没有 await 的 async 只会让调用方白白 await 一个
    永不挂起的协程。client 参数也删了：反馈是纯消费，不发任何 HTTP。

    收的是**原始 message 字符串**，不是 consume 解析后的 dict —— 那层解析会
    把 ``{"verdict": ...}`` 摊平成 dict 自身、顺手丢掉 ``message`` 键，回头再
    读 ``ev["message"]`` 只会拿到 None（写的时候真踩了这个坑）。
    """
    if not isinstance(body, str) or not body.strip():
        log("feedback empty body, ignored")
        return
    try:
        fb = json.loads(body)
        if not isinstance(fb, dict):
            raise ValueError("not an object")
    except Exception as e:
        # 手机上任何人都能往这个 topic 发东西，坏包要记下来但不能上抛
        log(f"feedback unparsable ({e}): {body[:80]}")
        return

    verdict = str(fb.get("verdict", "")).strip().lower()
    if verdict not in ("good", "bad"):
        log(f"feedback unknown verdict {verdict!r}, ignored")
        return

    pid = str(fb.get("push_id", "")).strip()
    pushes = st.setdefault("pushes", {})
    rec = pushes.pop(pid, None) if pid else None

    out = {"ts": time.time(), "verdict": verdict, "push_id": pid,
           "matched": rec is not None}
    src = ""
    if rec:
        out.update({k: rec.get(k) for k in
                    ("topic", "src", "content", "label", "reason")})
        src = rec.get("src") or ""
    _archive(out)

    # 规则更新失败不能吃掉这次反馈：归档已经在上面落地了，规则是额外收益。
    # 不包 try 的话磁盘满/权限问题会一路冒到 consume，日志会指向
    # "handle_event failed" —— 那行说的是另一个函数，排障会被带偏。
    if src:
        try:
            _apply_rule(src, verdict)
        except Exception as e:
            log(f"rule update failed for [{src}]: {e!r} (feedback archived ok)")

    mark = "👍准" if verdict == "good" else "👎误判"
    if rec:
        log(f"FEEDBACK {mark} matched={pid} src=[{src}] "
            f"label={rec.get('label')} | {(rec.get('content') or '')[:50]}")
    else:
        # 没匹配上：push_id 过期（7 天 TTL）、重复点击、或者手机上点的是
        # 很久以前的通知。判定仍然落盘，只是没有内容快照可用。
        # 注意未匹配的反馈**不改规则** —— 不知道它说的是哪条消息、哪个源，
        # 拿 verdict 去改任何源的计数都是瞎猜。
        log(f"FEEDBACK {mark} UNMATCHED push_id={pid or '(空)'}")
    # state 的落盘由调用方 consume 的 finally 统一做


def stats(since: float | None = None) -> dict:
    """给日报/排查用的汇总。只读 feedback.jsonl，不碰规则文件。"""
    since = since or (time.time() - 86400)
    out = {"good": 0, "bad": 0, "unmatched": 0, "by_src": {}}
    try:
        text = FEEDBACK_FILE.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("ts", 0) < since:
            continue
        v = rec.get("verdict")
        if not rec.get("matched"):
            out["unmatched"] += 1
        if v in ("good", "bad"):
            # bucket 的键恰好就是 good/bad —— 不必再判一次 v in bucket
            # （第 3 轮冗余）；顺带把建桶挪进这个 if，免得给非法 verdict
            # 塞一个 0/0 的空桶。
            out[v] += 1
            src = rec.get("src") or "?"
            out["by_src"].setdefault(src, {"good": 0, "bad": 0})[v] += 1
    return out


# ---------------------------------------------------------------- 命令行
# 拉黑不是单向门，但解锁需要一个正经入口：源被静音后不再产生新推送、
# 也就不再有新按钮可点，光靠「在旧通知上点👍」几乎走不通（要同时满足
# 7 天 TTL 内 + 手机上还没划掉）。这里给出人工解锁/查看的路径。
def _cli(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="fp_feedback",
        description="查看/修改误判反馈学到的源级规则")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出所有源的规则与计数")
    m = sub.add_parser("mute", help="手工静音一个源（跳过 AI）")
    m.add_argument("src")
    u = sub.add_parser("unmute", help="解除某个源的静音")
    u.add_argument("src")
    sub.add_parser("stats", help="近 24 小时反馈统计")
    a = p.parse_args(argv)

    if a.cmd == "list":
        sources = _load_rules().get("sources", {})
        if not sources:
            print("(空，还没有任何规则)")
        for name, e in sorted(sources.items()):
            state = "静音中" if e.get("verdict") == "ignore" else "正常"
            manual = " [手动]" if e.get("manual") else ""
            print(f"  {name:<16} {state:<6}{manual} bad={e.get('bad', 0)} "
                  f"good={e.get('good', 0)}")
        if sources:
            print("\n作用域：源级规则只作用于 Windows 通知（fp-pc）；"
                  "VPS 硬告警 / 灰区通道不受影响")
    elif a.cmd == "mute":
        rules = _load_rules()
        srcs = rules.setdefault("sources", {})
        old = srcs.get(a.src)
        old = old if isinstance(old, dict) else {}
        srcs[a.src] = {"bad": old.get("bad", 0), "good": old.get("good", 0),
                       "verdict": "ignore", "since": time.time(),
                       "manual": True}
        _save_rules(rules)
        # 不能只报「已静音」就完事：如果这个名字其实是 fp-vps 通道，闸门的
        # topic=="fp-pc" 根本不会查它，那是静默无效的假成功。
        print(f"已静音 [{a.src}]（manual，👍 不会自动撤销，需 unmute 解除）")
        print("作用域：仅 Windows 通知（fp-pc）。VPS 硬告警 / 灰区通道不受影响。")
    elif a.cmd == "unmute":
        rules = _load_rules()
        e = rules.get("sources", {}).get(a.src)
        if not isinstance(e, dict):
            print(f"[{a.src}] 没有规则，无需解除")
        else:
            e["verdict"] = None
            e["bad"] = 0          # 不清零的话 bad≥3 → 解除 → 下一条👎 立刻又静音，
            e.pop("manual", None)  # 解锁入口等于没加（第 2 轮 P2）
            _save_rules(rules)
            print(f"已解除 [{a.src}] 的静音，误判计数已清零")
    elif a.cmd == "stats":
        s = stats()
        print(f"近 24h：👍 {s['good']}  👎 {s['bad']}  "
              f"未匹配 {s['unmatched']}")
        for name, b in sorted(s["by_src"].items()):
            print(f"  {name:<16} 👍{b['good']} 👎{b['bad']}")
    else:
        # argparse 已经把未知子命令拦在前面了，走到这只可能是「加了新命令却
        # 忘了写分支」—— 那必须报错，不能静默打印统计（第 3 轮冗余）
        print(f"未知命令 {a.cmd!r}")
        return 2
    return 0


if __name__ == "__main__":
    import sys as _sys
    raise SystemExit(_cli(_sys.argv[1:]))
