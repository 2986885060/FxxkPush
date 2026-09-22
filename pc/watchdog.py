#!/usr/bin/env python3
"""FxxkPush 管道自检看门狗 —— 故障要推到手机，不能等有人开电脑看日志。

监控三件事，任一连续 FAIL_MIN 分钟不健康就直接推 fp-phone，绕过 AI：
  1. health  隧道 HTTP 200（隧道是全链路的地基）
  2. procs   5 个服务进程还在不在（shim+真身 = 2 个/服务）
  3. link    进程活着但连不上 —— 日志最近几分钟全是连接错误，
             正是 2026-09-22 那种"客户端中毒"的形态

告警通道必须独立于被监控对象：本地 127.0.0.1:2586 是隧道，隧道挂了它也挂。
所以先试本地（快），失败就用 SSH 到 VPS 上发 —— SSH 端口是这个网络唯一
放行的口，隧道死了它还活着。

2026-09-22 教训：隧道 11:33 断到 12:52，80 分钟零告警，因为告警通道和
被监控对象是同一条路。今天这个故障本该在 11:42 就推到手机上。
"""
from __future__ import annotations

import base64
import json
import re
import psutil
import time
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).parent
NTFY_BASE = "http://127.0.0.1:2586"
HEALTH_URL = f"{NTFY_BASE}/v1/health"
LOG_DIR = HERE / "logs"

CHECK_INTERVAL = 60        # 秒
FAIL_MIN = 5               # 连续不健康多少分钟才告警
LINK_ERR_MIN = 300         # link 检查回看多少秒
LINK_ERR_THRESHOLD = 3     # 这段时间内至少几条错误
RENOTIFY = 1800            # 同一故障持续时最多重复提醒的秒数（防刷屏）
HEARTBEAT_CYCLES = 10      # 每 10 轮（10 分钟）记一条心跳

WATCHED = [                # 被监控的 5 个服务（watchdog 不监控自己）
    "ntfy_tunnel.py",
    "pc_subscriber.py",
    "notification_listener.py",
    "ai_triager.py",
    "wechat_vision_listener.py",
]

import pclog
LOG = pclog.get_logger("watchdog")


# ---------------------------------------------------------------- 检查项
def _token() -> str:
    p = HERE / "ntfy.secret"
    return p.read_text().strip() if p.exists() else ""


def check_health() -> tuple[bool, str]:
    try:
        r = httpx.get(HEALTH_URL,
                      headers={"Authorization": f"Bearer {_token()}"},
                      timeout=6, trust_env=False)
        if r.status_code == 200:
            return True, "health=200"
        return False, f"health=HTTP {r.status_code}"
    except Exception as e:
        return False, f"health {type(e).__name__}: {e}"


def _our_pythons() -> list[str]:
    """本项目的 pythonw 命令行（只认 fuckpush\pc\ 下的，不碰 Hermes 的解释器）。

    为什么不用 PowerShell：watchdog 由 pythonw 启动、自己没有控制台，子进程
    powershell.exe 会新建一个前台窗口，每 60 秒闪一次约两秒。更糟的是
    PowerShell 一旦被安全软件拦截返回空，check_procs 会把 5 个服务全判成死，
    然后对着手机发一条纯误报的告警。psutil 是进程内遍历：无子进程、无窗口。

    process_iter 只取 name：cmdline 要逐个开进程句柄，对全系统几百个进程都取
    一次要 2 秒（还含大量 SYSTEM 进程 AccessDenied）；先按 name 过滤、再只对
    十几个 pythonw 取 cmdline，约 15ms。实测 15ms vs PowerShell 冷启动 3.5 秒。
    """
    out = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() != "pythonw.exe":
                continue
            cmd = " ".join(p.cmdline() or [])
            if "fuckpush\\pc\\" in cmd.lower():
                out.append(cmd)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return out


def check_procs() -> tuple[bool, str]:
    counts: dict[str, int] = {}
    for cl in _our_pythons():
        m = re.search(r"([a-z_]+\.py)", cl)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    dead = [s for s in WATCHED if counts.get(s, 0) < 2]
    if dead:
        return False, f"进程缺失: {', '.join(dead)} (计数={counts})"
    return True, f"{len(WATCHED)} 服务进程齐"


def _log_ts(line: str) -> float | None:
    """解析 pclog 时间戳 `2026-09-22T13:09:05.258+0800` -> epoch 秒。

    只取前 19 位按本地时间算 —— 我们只关心时间差，不跨时区。
    """
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


_ERR_PAT = re.compile(
    r"\[ERROR\]|ConnectError|10061|stream error|All connection attempts|"
    r"连接被拒绝|ReadTimeout|ConnectTimeout|peer closed", re.I)


def check_link() -> tuple[bool, str]:
    """进程活着但连不上：最近 LINK_ERR_MIN 秒里错误刷屏、且没有成功日志。

    抓的是 2026-09-22 那种形态 —— subscriber 进程健在、每 7 秒一条
    ConnectError，健康检查（隧道）却是绿的，只有日志知道它废了。
    """
    now = time.time()
    suspects = []
    for svc in ("pc_subscriber", "ai_triager"):
        p = LOG_DIR / f"{svc}.log"
        if not p.exists():
            continue
        # 只读尾部：原来是 read_text().splitlines()[-60:] —— 为了 60 行
        # 把整个日志读进来。2MB 轮转阈值 x 2 个文件 x 每 60 秒一次 ≈ 5.7GB/天
        # 的纯磁盘读，全喂给页缓存。
        lines = pclog.read_tail(p, 60)
        if not lines:
            continue
        errs = oks = 0
        for ln in lines:
            ts = _log_ts(ln)
            if ts is None or now - ts > LINK_ERR_MIN:
                continue
            if _ERR_PAT.search(ln):
                errs += 1
            elif "[INFO" in ln or "[WARN" in ln:
                oks += 1
        if errs >= LINK_ERR_THRESHOLD and oks == 0:
            suspects.append(f"{svc}: {errs} 条错误/0 条正常（近 {LINK_ERR_MIN//60}min）")
    if suspects:
        return False, "link " + "; ".join(suspects)
    return True, "link 正常"


CHECKS = [("health", check_health), ("procs", check_procs), ("link", check_link)]


# ------------------------------------------------- 告警通道（独立于隧道）
def _ntfy_payload(title: str, message: str) -> dict:
    # 告警自带一个新 trace，但必须用 with 恢复 —— 直接 set_trace_id(None)
    # 会把 trace 留在上下文里，之后的心跳日志全都挂上最后一次告警的 id，
    # grep 心跳时会串到无关的告警上。
    with pclog.trace(None):
        return {
            "topic": "fp-phone",
            "title": title[:120],
            "message": message[:900],
            "priority": 4,                 # hard rule: high, 不经 AI
            "tags": pclog.tags_with_trace(["bell", "rotating_light"]),
        }


def push_local(payload: dict) -> bool:
    """本地隧道（快）。隧道挂了这里必然失败 —— 由 push_vps 兜底。"""
    try:
        r = httpx.post(f"{NTFY_BASE}/", json=payload,
                       headers={"Authorization": f"Bearer {_token()}"},
                       timeout=8, trust_env=False)
        return r.status_code == 200
    except Exception as e:
        LOG.warning(f"local alert channel failed: {e!r}")
        return False


def push_vps(payload: dict) -> bool:
    """SSH 到 VPS，在它本机发 ntfy —— 隧道死了这条路还活着。

    payload 走 base64：JSON 里有中文和引号，塞进 shell 命令会被转义啃掉，
    base64 全 ASCII 就没这问题；VPS 端用 curl -d @file 读，零转义。
    """
    secret = HERE.parent / "vps.secret"
    if not secret.exists():
        LOG.error("vps.secret 不存在，无法走 SSH 兜底")
        return False
    host, port, user, pwd = secret.read_text().split()
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, port=int(port), username=user, password=pwd,
                    timeout=15, banner_timeout=20, auth_timeout=20)
        b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
        # curl 走 shell 命令行，两个约束：整条命令的单引号必须是偶数
        # （否则 shell 报未闭合、$() 拿到空串、push_vps 恒返回 False），
        # 以及 token 只在取得到时才带 header。token 出现在命令行只在自己的
        # VPS 上短暂可见（ps aux），可接受。
        tok = _token()
        auth = f"-H 'Authorization: Bearer {tok}' " if tok else ""
        cmd = (
            "set -e; "
            f"echo {b64} | base64 -d > /tmp/fp_watchdog_alert.json; "
            f"code=$(curl -s -o /dev/null -w '%{{http_code}}' "
            f"{auth}"
            "-H 'Content-Type: application/json' "
            "-d @/tmp/fp_watchdog_alert.json "
            "http://127.0.0.1:2586/); "
            "rm -f /tmp/fp_watchdog_alert.json; "
            "echo $code"
        )
        _, stdout, stderr = cli.exec_command(cmd, timeout=25)
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        if out == "200":
            return True
        LOG.error(f"vps alert rejected: code={out!r} stderr={err[:200]!r}")
        return False
    except Exception as e:
        LOG.error(f"vps alert channel failed: {e!r}")
        return False
    finally:
        try:
            cli.close()
        except Exception:
            pass


def send_alert(title: str, message: str) -> bool:
    """先本地（隧道健康时秒回），失败走 SSH。任一成功即算送达。"""
    payload = _ntfy_payload(title, message)
    if push_local(payload):
        LOG.info(f"告警已送达(本地) {title}")
        return True
    if push_vps(payload):
        LOG.info(f"告警已送达(VPS-SSH兜底) {title}")
        return True
    LOG.error(f"告警两条通道都失败！{title}")
    return False


# ---------------------------------------------------------------- 主循环
def fmt_dur(sec: float) -> str:
    m = int(sec // 60)
    return f"{m}分钟" if m >= 1 else f"{int(sec)}秒"


def main() -> int:
    LOG.info(f"watchdog up: interval={CHECK_INTERVAL}s fail_threshold={FAIL_MIN}min")
    fault_since: dict[str, float] = {}
    last_alert: dict[str, float] = {}   # name -> 上次告警时间
    cycle = 0

    while True:
        cycle += 1
        now = time.time()
        healthy_now = 0
        for name, fn in CHECKS:
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, f"检查器异常 {type(e).__name__}: {e}"

            if ok:
                healthy_now += 1
                if name in fault_since:
                    dur = now - fault_since.pop(name)
                    LOG.info(f"{name} 恢复（故障持续 {fmt_dur(dur)}）")
                    if name in last_alert:
                        # 告警过就告知恢复，否则静默（没打扰过就别吵）
                        send_alert(f"[FxxkPush] {name} 已恢复",
                                   f"故障持续 {fmt_dur(dur)} 后恢复正常。\n{detail}")
                        last_alert.pop(name, None)
                continue

            # 不健康
            start = fault_since.setdefault(name, now)
            dur = now - start
            if dur < FAIL_MIN * 60:
                LOG.warning(f"{name} 不健康 {fmt_dur(dur)}（阈值 {FAIL_MIN}min）: {detail}")
                continue

            # 超过阈值：告警（同一故障最多每 RENOTIFY 秒重复一次）
            prev = last_alert.get(name, 0)
            if now - prev < RENOTIFY:
                continue
            last_alert[name] = now
            started = datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S")
            msg = (f"检查项: {name}\n"
                   f"详情: {detail}\n"
                   f"已持续: {fmt_dur(dur)}（阈值 {FAIL_MIN} 分钟）\n"
                   f"首次发现: {started}\n"
                   f"影响: {'全链路推送中断' if name == 'health' else '对应服务不可用'}\n"
                   f"排查: pc/logs/*.log")
            LOG.error(f"告警 {name}: {detail}")
            send_alert(f"[FxxkPush] {name} 故障 {fmt_dur(dur)}", msg)

        # 心跳：全绿时也留痕迹，否则 watchdog 静默运行时分不清它是健康还是已经死了
        if healthy_now == len(CHECKS) and cycle % HEARTBEAT_CYCLES == 0:
            LOG.info(f"heartbeat: {len(CHECKS)}/{len(CHECKS)} 检查通过 "
                     f"(health/procs/link) 第{cycle}轮")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
