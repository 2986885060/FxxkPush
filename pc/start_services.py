#!/usr/bin/env python3
"""FxxkPush 启动编排：先隧道 -> 验 health=200 -> 才起其余 -> 校验进程数。

为什么需要它：restart_services.bat 原来是 `timeout /t 3` 盲等隧道。隧道慢
一点（SSH 握手抖动、代理切换、开机网络未就绪），后面四个服务就会对着一个还
没起来的 2586 狂连，而且没有任何报错提示是"启动顺序"问题 —— 2026-09-22 那次
70 分钟的静默故障就是这么起来的。

用法：
    python start_services.py          # 前台跑，看得到每一步
    restart_services.bat              # 包装它（双击入口）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

HERE = Path(__file__).parent
PYW = HERE.parent / ".venv" / "Scripts" / "pythonw.exe"
NTFY_BASE = "http://127.0.0.1:2586"
HEALTH_URL = f"{NTFY_BASE}/v1/health"
HEALTH_TIMEOUT = 60        # 秒：隧道要多久才算真起不来
POST_HEALTH_DELAY = 1.5    # health 200 后给隧道一点热身时间

import pclog
LOG = pclog.get_logger("start_services")

# 顺序就是依赖：隧道必须先通，其余才连得上；watchdog 最后（它监控前面所有）
ORDER = [
    "ntfy_tunnel.py",
    "pc_subscriber.py",
    "notification_listener.py",
    "ai_triager.py",
    "wechat_vision_listener.py",
    "watchdog.py",
]
EXPECT_PER_SCRIPT = 2       # venv pythonw.exe 是 shim，真身另起一个：合法 2 个


def _token() -> str:
    p = HERE / "ntfy.secret"
    return p.read_text().strip() if p.exists() else ""


def _our_procs(include_self: bool = True) -> list[tuple[int, str]]:
    """[(pid, cmdline), ...] 本项目的 pythonw（shim + 真身都算）。

    psutil 而非 PowerShell：子进程会弹控制台窗口（pythonw 父进程无控制台时），
    且进程内遍历约 15ms，PowerShell 冷启动要 1-5 秒。
    process_iter 只取 name（便宜），cmdline 单独对 pythonw 取 —— 全量取 cmdline
    是 2 秒级，对全系统几百个进程都开一遍句柄。
    """
    out = []
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() != "pythonw.exe":
                continue
            try:
                cmd = " ".join(proc.cmdline() or [])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            low = cmd.lower()
            if "fuckpush\\pc\\" not in low:
                continue          # 只动自己，Hermes 等其它 pythonw 不碰
            if not include_self and "start_services" in low:
                continue          # 不杀自己
            out.append((proc.pid, cmd))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return out


def kill_ours() -> list[str]:
    """杀掉本项目全部 pythonw（shim + 真身）。返回未退出的残留 cmdline。"""
    procs = _our_procs(include_self=False)
    if procs:
        LOG.info(f"终止 {len(procs)} 个旧进程")
    for pid, _ in procs:
        try:
            psutil.Process(pid).kill()
        except psutil.Error as e:
            LOG.warning(f"kill pid={pid} failed: {e!r}")
    # 等进程真的退出，否则新旧同脚本并存会双写日志
    for _ in range(20):
        if not _our_procs(include_self=False):
            break
        time.sleep(0.3)
    left = _our_procs(include_self=False)
    if left:
        LOG.warning(f"仍有 {len(left)} 个残留进程未退出")
    return [c for _, c in left]


def list_ours() -> list[str]:
    """返回本项目 pythonw 的 cmdline 列表（含 start_services 自己）。"""
    return [c for _, c in _our_procs(include_self=True)]


def start(script: str) -> None:
    LOG.info(f"启动 {script}")
    subprocess.Popen([str(PYW), str(HERE / script)],
                     cwd=str(HERE),
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)


def wait_health(timeout: int = HEALTH_TIMEOUT) -> tuple[bool, str]:
    """轮询隧道 health，直到 200 或超时。返回 (ok, 详情)。"""
    tok = _token()
    deadline = time.time() + timeout
    last = "未尝试"
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = httpx.get(HEALTH_URL,
                          headers={"Authorization": f"Bearer {tok}"} if tok else {},
                          timeout=5, trust_env=False)
            last = f"HTTP {r.status_code}"
            if r.status_code == 200:
                return True, f"attempt={attempt} {last}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5)
    return False, f"attempt={attempt} 最后一次: {last}"


def tail(path: Path, n: int = 8) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except Exception:
        return "(读不到)"


def verify_procs() -> dict[str, int]:
    """按脚本名统计进程数；2 = shim + 真身，正常。"""
    counts: dict[str, int] = {}
    for cl in list_ours():
        m = re.search(r"([a-z_]+\.py)", cl)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def main() -> int:
    LOG.info("=" * 60)
    LOG.info("FxxkPush 启动编排开始")
    pclog.set_trace_id(None)

    # 1) 清场
    LOG.info("停掉旧进程")
    kill_ours()
    time.sleep(1.0)

    # 2) 隧道先行 + 健康门（核心：不验 health=200 就不起后面）
    LOG.info(f"启动隧道并等待 health（最多 {HEALTH_TIMEOUT}s）")
    start(ORDER[0])
    ok, detail = wait_health()
    if not ok:
        LOG.error(f"隧道健康检查失败，中止启动：{detail}")
        LOG.error("隧道日志最后几行:\n" + tail(HERE / "logs" / "ntfy_tunnel.log"))
        print(f"\n[失败] 隧道 health 未通过（{HEALTH_TIMEOUT}s 内）：{detail}")
        print("后面的服务已按要求不起 —— 没有隧道，它们只会空转刷错。")
        print("排查: logs/ntfy_tunnel.log  |  vps.secret 是否正确 / SSH 端口是否通")
        return 2
    LOG.info(f"隧道 health=200（{detail}）")
    time.sleep(POST_HEALTH_DELAY)

    # 3) 其余服务（隧道已确认可用）
    for script in ORDER[1:]:
        start(script)
        time.sleep(0.4)

    # 4) 进程自检
    time.sleep(3.0)
    counts = verify_procs()
    bad = {s: c for s, c in counts.items()
           if s in ORDER and c < EXPECT_PER_SCRIPT}
    missing = [s for s in ORDER if s not in counts]

    LOG.info(f"进程统计: {counts}")
    if bad or missing:
        LOG.error(f"异常：缺={missing} 不足={bad}")
        print("\n[警告] 部分服务进程数不对：")
        for s in missing:
            print(f"  - {s}: 没起来")
        for s, c in bad.items():
            print(f"  - {s}: 只有 {c} 个（期望 {EXPECT_PER_SCRIPT}）")
        print("\n看对应日志: pc/logs/<服务名>.log")
        return 1

    total = sum(counts.get(s, 0) for s in ORDER)
    LOG.info(f"全部就绪: {len(ORDER)}/{len(ORDER)} 服务, {total} 个进程")
    print(f"\n[OK] {len(ORDER)} 个服务全部就绪，共 {total} 个进程（shim+真身）。")
    print(f"     隧道 health=200 | 日志目录: {HERE / 'logs'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
