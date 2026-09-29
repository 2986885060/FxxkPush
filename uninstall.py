#!/usr/bin/env python3
"""FxxkPush 一键卸载（纯标准库，与 setup.bat 向导对称）。

用法：
    uninstall.py                 交互确认后卸载（数据默认保留）
    uninstall.py --yes           免确认（脚本化）
    uninstall.py --purge         连 fp.config.json 与运行数据一并删除（不可恢复）
    uninstall.py --dry-run       只打印将执行的动作，不改动任何东西

卸载动作（顺序固定）：
  1. 停止全部 FxxkPush 服务进程（按命令行匹配 fuckpush/pc，不动其它 pythonw）
  2. 删除 HKCU Run 的 6 个 FuckPush_* 自启项（含 StartupApproved 残留）
  3. 把挪到屏外的聊天窗口拉回屏内（vision_listener unpark）
  4.（--purge）删除 fp.config.json、pc/logs 与运行状态数据

不会碰：仓库代码、.venv、其它 .secret（cf/maddy/vps110 等非本项目文件）、
VPS 侧任何东西（停用 VPS 见结尾提示）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import winreg
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _detect_root() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (Path.cwd().resolve(), here):
        if (cand / "pc" / "services" / "ai_triager.py").exists():
            return cand
    sys.exit("[失败] 当前目录和脚本目录都不是 FxxkPush 项目根"
             "（缺 pc\\services\\ai_triager.py）")


ROOT = _detect_root()
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SA_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
AUTOSTART = {
    "ntfy_tunnel": "FuckPush_tunnel",
    "pc_subscriber": "FuckPush_subscriber",
    "notification_listener": "FuckPush_listener",
    "ai_triager": "FuckPush_triager",
    "vision_listener": "FuckPush_wechat_vision",
    "watchdog": "FuckPush_watchdog",
}

# 只按命令行特征匹配本项目进程，绝不按镜像名一刀切（会误杀其它 pythonw）
_PS_LIST = (
    'Get-CimInstance Win32_Process -Filter "Name=\'pythonw.exe\'" | '
    "Where-Object { $_.CommandLine -match 'fuckpush[\\\\/]pc[\\\\/]' } | "
    "ForEach-Object { Write-Output $_.ProcessId }"
)
_PS_KILL = (
    'Get-CimInstance Win32_Process -Filter "Name=\'pythonw.exe\'" | '
    "Where-Object { $_.CommandLine -match 'fuckpush[\\\\/]pc[\\\\/]' } | "
    "ForEach-Object { Write-Output $_.ProcessId; "
    "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
)

PURGE_FILES = [
    "fp.config.json",
    "pc/logs",                                    # 目录整体
    "pc/feedback.jsonl",
    "pc/notifications.jsonl",
    "pc/toast_echo.jsonl",
    "pc/triage_log.jsonl",
    "pc/vision_log.jsonl",
    "pc/listener_state.json",
    "pc/triage_state.json",
    "pc/vision_state.json",
    "pc/triage_rules.json",
    "pc/bootstrap/.start_services.lock",
]


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    say(f"  [OK] {msg}")


def warn(msg: str) -> None:
    say(f"  [!!] {msg}")


def run_ps(cmd: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:                                  # noqa: BLE001
        return 1, repr(e)


def list_service_pids() -> list[str]:
    _, out = run_ps(_PS_LIST)
    return [ln.strip() for ln in out.splitlines() if ln.strip().isdigit()]


def step_stop(dry: bool) -> bool:
    say("\n== 1/3 停止服务进程 ==")
    pids = list_service_pids()
    if not pids:
        ok("无运行中的服务进程")
        return True
    if dry:
        say(f"  [dry] 将结束 {len(pids)} 个进程: {', '.join(pids)}")
        return True
    _, out = run_ps(_PS_KILL)
    killed = [ln.strip() for ln in out.splitlines() if ln.strip().isdigit()]
    # 给进程退出一点时间
    import time
    time.sleep(1.5)
    left = list_service_pids()
    if left:
        fail(f"仍有 {len(left)} 个进程未退出: {', '.join(left)}")
        return False
    ok(f"已结束 {len(killed)} 个服务进程")
    return True


def step_autostart(dry: bool) -> bool:
    say("\n== 2/3 移除开机自启 ==")
    names: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            i = 0
            while True:
                try:
                    name, _, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if name in AUTOSTART.values():
                    names.append(name)
    except OSError as e:
        fail(f"读取 Run 键失败: {e!r}")
        return False
    if not names:
        ok("无 FuckPush_* 自启项（可能已移除）")
        return True
    if dry:
        say(f"  [dry] 将删除 {len(names)} 项: {', '.join(names)}")
        return True
    removed = 0
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            for name in names:
                try:
                    winreg.DeleteValue(key, name)
                    removed += 1
                except FileNotFoundError:
                    pass
    except OSError as e:
        fail(f"删除 Run 值失败: {e!r}")
        return False
    # StartupApproved 同名残留（任务管理器里勾选状态），尽力清理
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SA_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            for name in names:
                try:
                    winreg.DeleteValue(key, name)
                except OSError:
                    pass
    except OSError:
        pass
    ok(f"已删除 {removed}/{len(names)} 项自启（含 StartupApproved 残留清理）")
    return True


def step_unpark(dry: bool) -> bool:
    say("\n== 3/3 聊天窗口拉回屏内 ==")
    vision = ROOT / "pc" / "services" / "vision_listener.py"
    if not VENV_PY.exists():
        warn(".venv 不存在，跳过（窗口可能仍在屏外；装回后可运行 "
             "vision_listener.py unpark）")
        return True
    if dry:
        say(f"  [dry] 将运行: {VENV_PY.name} vision_listener.py unpark")
        return True
    try:
        p = subprocess.run([str(VENV_PY), str(vision), "unpark"],
                           input="\n", capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           cwd=str(ROOT), timeout=120)
        tail = [ln for ln in ((p.stdout or "") + (p.stderr or "")).splitlines()
                if ln.strip()]
        for ln in tail[-8:]:
            say("  " + ln)
        if p.returncode != 0:
            warn(f"unpark 退出码 {p.returncode}（窗口没开的话属正常）")
        else:
            ok("窗口归位完成")
        return True
    except Exception as e:                                  # noqa: BLE001
        warn(f"unpark 执行失败: {e!r}")
        return False


def step_purge(dry: bool) -> bool:
    say("\n== 4/4 清除配置与运行数据（--purge）==")
    targets = [ROOT / rel for rel in PURGE_FILES]
    existing = [p for p in targets if p.exists()]
    if not existing:
        ok("无待删除文件")
        return True
    if dry:
        for p in existing:
            say(f"  [dry] 将删除 {p.relative_to(ROOT)}")
        return True
    for p in existing:
        try:
            if p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        except OSError as e:
            warn(f"删除失败 {p.name}: {e!r}")
    ok(f"已清除 {len(existing)} 项（配置/日志/状态）")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="FxxkPush 一键卸载")
    ap.add_argument("--yes", action="store_true", help="免交互确认")
    ap.add_argument("--purge", action="store_true",
                    help="连 fp.config.json 与运行数据一起删（不可恢复）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不改动")
    args = ap.parse_args()

    say("=" * 52)
    say("  FxxkPush 一键卸载" + ("（dry-run，只看不改）" if args.dry_run else ""))
    say(f"  项目根: {ROOT}")
    say("=" * 52)
    say("将执行：停服务 → 摘自启 → 窗口回屏内" +
        (" → 清除配置与数据" if args.purge else "（数据保留）"))

    purge = args.purge
    if not args.dry_run and not args.yes:
        try:
            ans = input("确认卸载？输入 yes 继续: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            say("\n已取消")
            return 130
        if ans != "yes":
            say("已取消（未做任何改动）")
            return 130
        if purge:
            try:
                ans2 = input("purge 将删除 fp.config.json（含密钥）与全部运行"
                             "数据，不可恢复！输入 purge 确认: ").strip()
            except (EOFError, KeyboardInterrupt):
                say("\n已取消")
                return 130
            if ans2 != "purge":
                purge = False
                warn("取消 purge，仅执行基础卸载（数据保留）")

    all_ok = True
    all_ok &= step_stop(args.dry_run)
    all_ok &= step_autostart(args.dry_run)
    all_ok &= step_unpark(args.dry_run)
    if purge:
        all_ok &= step_purge(args.dry_run)

    say("\n================ 卸载结果 ================")
    say("  " + ("全部完成" if all_ok else "部分步骤失败（见上方 [!!]/[X ]）"))
    if not purge:
        say("  数据保留: fp.config.json / pc/logs / 运行状态（重装直接复用）")
    say("  重装: 双击 setup.bat")
    say("  VPS 侧不受影响；如需停用：")
    say("    systemctl disable --now fuckpush-monitor ntfy")
    say("==========================================")
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n已中断")
        sys.exit(130)
