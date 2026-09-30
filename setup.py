#!/usr/bin/env python3
"""FxxkPush 一键安装向导（纯标准库，任意 Python 3.10+ 可运行）。

双击 setup.bat（或 `uv run --python 3.12 setup.py`）即可，向导按序完成：
  1. 检测项目根（当前目录优先，脚本目录回退）
  2. 创建 .venv 并安装依赖（优先 uv，其次 python -m venv + pip）
  3. 问答式生成 fp.config.json（已存在默认跳过，绝不静默覆盖）
  4. Windows 通知读取授权
  5. 注册 6 项开机自启（HKCU Run）
  6. 立即启动服务栈（start_services 健康门）
  7. 把配置的聊天窗口挪到屏外（可跳过）
  8. 健康验证 + 汇总

用法：
    setup.py            交互安装
    setup.py --check    只检查环境/配置/自启/健康，不改动任何东西
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _detect_root() -> Path:
    """当前目录优先（bat 会 pushd 到项目根），脚本目录回退。"""
    here = Path(__file__).resolve().parent
    for cand in (Path.cwd().resolve(), here):
        if (cand / "pc" / "services" / "ai_triager.py").exists():
            return cand
    sys.exit("[失败] 当前目录和脚本目录都不是 FxxkPush 项目根"
             "（缺 pc\\services\\ai_triager.py）")


ROOT = _detect_root()
VENV = ROOT / ".venv"
VPY = VENV / "Scripts" / "python.exe"
CONFIG = ROOT / "fp.config.json"

DEPS = [
    "httpx", "winotify", "paramiko", "psutil", "pillow",
    "winrt-runtime",
    "winrt-Windows.UI.Notifications.Management",
    "winrt-Windows.UI.Notifications",
    "winrt-Windows.Foundation",
    "winrt-Windows.ApplicationModel",
    "winrt-Windows.Foundation.Collections",
]

# 服务脚本 -> HKCU Run 值名（与 注册自启动.bat 保持一致）
AUTOSTART = {
    "ntfy_tunnel": "FuckPush_tunnel",
    "pc_subscriber": "FuckPush_subscriber",
    "notification_listener": "FuckPush_listener",
    "ai_triager": "FuckPush_triager",
    "vision_listener": "FuckPush_wechat_vision",
    "watchdog": "FuckPush_watchdog",
}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---------------- 基础工具 ----------------
def say(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    say(f"  [OK] {msg}")


def warn(msg: str) -> None:
    say(f"  [!!] {msg}")


def fail(msg: str) -> None:
    say(f"  [X ] {msg}")


def ask(label: str, default: str | None = None, secret: bool = False) -> str:
    """带默认值问答（直接回车=默认）；EOF/管道输入时自动用默认，便于无人值守。"""
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        raw = getpass.getpass(f"{label}{suffix}: ") if secret else input(f"{label}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        say()
        raw = ""
    raw = raw.strip()
    return raw if raw else ("" if default is None else str(default))


def ask_yn(label: str, default: str = "y") -> bool:
    return ask(f"{label} (y/n)", default).strip().lower() in ("y", "yes", "")


def tail(text: str, n: int = 14) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def run(cmd: list[str], *, timeout: int = 600, stdin_text: str | None = None,
        cwd: Path | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(
            [str(c) for c in cmd], cwd=str(cwd or ROOT), input=stdin_text,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    except Exception as e:                                  # noqa: BLE001
        return 1, repr(e)


def which_python_for_venv() -> list[str] | None:
    """给 python -m venv 用的解释器探测（uv 不存在时的回退）。"""
    for cand in (["py", "-3"], ["python"], ["python3"]):
        rc, out = run(cand + ["--version"], timeout=20)
        if rc == 0 and out.strip().startswith("Python 3"):
            return cand
    return None


# ---------------- 步骤1：venv + 依赖 ----------------
def deps_missing() -> list[str]:
    if not VPY.exists():
        return ["<venv>"]
    code = (
        "import importlib.util as u\n"
        "mods=['httpx','winotify','paramiko','psutil','PIL','winrt.runtime']\n"
        "miss=[]\n"
        "for m in mods:\n"
        "    try:\n"
        "        if u.find_spec(m) is None: miss.append(m)\n"
        "    except Exception: miss.append(m)\n"
        "print(','.join(miss))"
    )
    rc, out = run([str(VPY), "-c", code], timeout=60)
    if rc != 0:
        return ["<venv运行失败>"]
    return [m for m in out.strip().split(",") if m]


def step_deps() -> bool:
    say("\n== 1/7 虚拟环境与依赖 ==")
    miss = deps_missing()
    if not miss:
        ok(f".venv 就绪，依赖齐全（{len(DEPS)} 项）")
        return True

    if not VENV.exists():
        say("  创建 .venv（Python 3.12）...")
        if shutil.which("uv"):
            # str(VENV) 而非字面 ".venv"：生产等价（VENV==ROOT/.venv），
            # 但单测把 VENV 打桩到临时目录时才不会动真 .venv
            rc, out = run(["uv", "venv", str(VENV), "--python", "3.12"], timeout=300)
        else:
            py = which_python_for_venv()
            if py is None:
                fail("找不到可用的 Python 3，也没检测到 uv")
                say("      请先安装 Python 3.12+（或 uv：https://uv.sh）后重跑 setup.bat")
                return False
            rc, out = run(py + ["-m", "venv", str(VENV)], timeout=300)
        if rc != 0 or not VPY.exists():
            fail("创建 venv 失败：")
            say(tail(out))
            return False
        ok(".venv 已创建")

    if deps_missing():
        say("  安装依赖（httpx/winotify/paramiko/psutil/pillow/winrt-*，可能需要几分钟）...")
        if shutil.which("uv"):
            rc, out = run(["uv", "pip", "install", "--python", str(VPY)] + DEPS,
                          timeout=900)
        else:
            rc, out = run([str(VPY), "-m", "pip", "install", "--upgrade", "pip"],
                          timeout=300)
            if rc == 0:
                rc, out = run([str(VPY), "-m", "pip", "install"] + DEPS, timeout=900)
        miss = deps_missing()
        if miss:
            fail(f"依赖安装未完成，缺: {', '.join(miss)}")
            say(tail(out))
            return False
    ok("依赖安装完成")
    return True


# ---------------- 步骤2：fp.config.json ----------------
def _default_vision(my_name: str) -> dict:
    return {
        "settings": {
            "poll_min": 30, "my_name": my_name, "active_from": 8,
            "active_to": 24, "dedup_ttl_sec": 21600, "offscreen_offset": 120,
        },
        "wechat": {
            "process": "Weixin.exe", "class": "Qt51514", "title": None,
            "min_w": 400, "enabled": True, "label": "微信",
            "prompt_hint": "左侧为会话列表，红色数字气泡为未读",
        },
        "wecom": {
            "process": "WXWork.exe", "class": "WeWorkWindow", "title": "企业微信",
            "min_w": 500, "enabled": True, "label": "企业微信",
            "prompt_hint": "左侧为会话列表，红色数字气泡为未读",
        },
        # QQ 不进视觉清单：QQ 走 Windows toast 主通道（秒级），窗口最小化到
        # 托盘时视觉侧会误报「采集停摆」。QQ 的分诊规则在 fp.config apps 块。
    }


def _default_apps() -> dict:
    return {
        "_default": {
            "ignore": False, "prompt_hint": "", "must_push": [],
            "blacklist": ["微信支付", "公众号", "服务通知", "QQ邮箱提醒",
                          "折叠的聊天", "应用提醒", "失物招领&寻物启事"],
        },
        "QQ": {"must_push": ["取件码"]},
    }


def step_config() -> bool:
    say("\n== 2/7 生成 fp.config.json ==")
    if CONFIG.exists():
        if not ask_yn("检测到已有 fp.config.json，重新生成并覆盖", "n"):
            ok("保留现有配置")
            return True

    say("  （直接回车 = 用方括号里的默认值）")
    host = ask("VPS 公网 IP / 域名").strip()
    host_tries = 0
    while not host:
        # 上限防止 EOF/管道输入下永远拿到空串 → 死循环
        host_tries += 1
        if host_tries >= 5:
            fail("VPS 地址连续 5 次为空（或输入已关闭），中止配置")
            return False
        host = ask("VPS 公网 IP / 域名（必填）").strip()
    port_tries = 0
    while True:
        port = ask("SSH 端口", "22")
        if port.isdigit() and 0 < int(port) < 65536:
            break
        port_tries += 1
        if port_tries >= 5:
            fail("端口连续 5 次非法，中止配置")
            return False
        warn("端口需为数字")
    user = ask("SSH 用户", "root")
    password = ask("SSH 密码（隧道/兜底推送用；纯密钥登录可留空）", secret=True)
    token = ask("ntfy token（VPS 脚本输出的 tk_ 开头那串）", secret=True)
    say("  --- AI / OCR 模型（三处分诊共用；留空将触发 fail-open 全量推送）---")
    base_url = ask("AI 接口地址 base_url", "").rstrip("/")
    api_key = ask("AI api_key", secret=True)
    model = ask("AI 模型名", "")
    my_name = ask("你的昵称（用于「@我」判定）", "你的昵称")

    try:
        port_i = int(port)
    except ValueError:
        fail("端口非法")
        return False

    cfg = {
        "ai": {
            "provider": "", "base_url": base_url, "api_key": api_key,
            "model": model, "max_tokens": 200, "max_completion_tokens": 600,
            "dedup_window_sec": 1800, "daily_report_time": "22:00",
        },
        "ntfy": {"token": token},
        "vps": {"host": host, "port": port_i, "user": user, "password": password},
        "vision": _default_vision(my_name),
        "apps": _default_apps(),
    }
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    # 回读校验（防写坏）
    back = json.loads(CONFIG.read_text(encoding="utf-8"))
    need = ("ai", "ntfy", "vps", "vision", "apps")
    if any(k not in back for k in need):
        fail("配置回读校验失败")
        return False
    if not base_url or not model:
        warn("AI 地址/模型为空：AI 分诊将 fail-open（判不了就全推），建议补全")
    ok(f"已写入 {CONFIG}")
    return True


# ---------------- 步骤3：通知授权 ----------------
def step_access() -> bool:
    say("\n== 3/7 Windows 通知读取授权 ==")
    if not ask_yn("现在授权（会弹系统窗口，请点「允许」；已授权过可直接回车）", "y"):
        ok("跳过")
        return True
    rc, out = run([str(VPY), str(ROOT / "pc" / "services" / "notification_listener.py"),
                   "--grant-access"], timeout=180)
    say(tail(out))
    if rc != 0:
        warn("授权未确认成功——toast 通道可能不可用；之后可手动执行：")
        say("      .venv\\Scripts\\python.exe pc\\services\\notification_listener.py --grant-access")
        return False
    ok("通知授权完成")
    return True


# ---------------- 步骤4：注册自启动 ----------------
def step_autostart() -> bool:
    say("\n== 4/7 注册开机自启（HKCU Run，6 项）==")
    if not VPY.exists():
        fail(".venv 不存在，无法注册")
        return False
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY,
                                0, winreg.KEY_SET_VALUE) as key:
            for script, reg_name in AUTOSTART.items():
                value = f'"{VPY}" "{ROOT / "pc" / "services" / (script + ".py")}"'
                winreg.SetValueEx(key, reg_name, 0, winreg.REG_SZ, value)
    except Exception as e:                                  # noqa: BLE001
        fail(f"写注册表失败: {e!r}")
        return False
    # 回读校验：6 项存在且路径可达
    good = 0
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if name in AUTOSTART.values():
                    parts = value.strip('"').split('" "')
                    if all(Path(p.replace("\\\\", "\\")).exists() for p in parts[:2]):
                        good += 1
    except Exception as e:                                  # noqa: BLE001
        fail(f"回读校验失败: {e!r}")
        return False
    if good != len(AUTOSTART):
        fail(f"自启项校验 {good}/{len(AUTOSTART)}")
        return False
    ok("6/6 自启项已注册（下次登录生效）")
    return True


# ---------------- 步骤5：立即启动 ----------------
def step_start() -> bool:
    say("\n== 5/7 立即启动服务栈（杀旧 → 隧道健康门 → 起服务 → 校验）==")
    rc, out = run([str(VPY), str(ROOT / "pc" / "bootstrap" / "start_services.py")],
                  timeout=240)
    say(tail(out, 16))
    if rc != 0:
        fail(f"启动失败（exit={rc}），看 pc\\logs\\start_services.log 与 ntfy_tunnel.log")
        return False
    ok("6/6 服务就绪")
    return True


# ---------------- 步骤6：拖走聊天窗口 ----------------
def step_park() -> bool:
    say("\n== 6/7 窗口归位（挪到屏外，视觉通道需要）==")
    if not ask_yn("把配置的聊天窗口（微信/企微/QQ）挪到屏幕外", "y"):
        ok("跳过（之后随时可双击「拖走聊天窗口.bat」）")
        return True
    rc, out = run([str(VPY), str(ROOT / "pc" / "services" / "vision_listener.py"),
                   "park"], stdin_text="\n", timeout=120)
    say(tail(out, 12))
    if rc != 0:
        warn("窗口归位未确认成功（应用没开的话属正常，之后会自动归位）")
    else:
        ok("窗口归位完成")
    return True


# ---------------- 步骤7：验证 ----------------
def step_verify() -> bool:
    say("\n== 7/7 健康验证 ==")
    try:
        with urllib.request.urlopen("http://127.0.0.1:2586/v1/health", timeout=8) as r:
            code = r.status
    except Exception as e:                                  # noqa: BLE001
        fail(f"隧道健康检查失败: {e!r}")
        say("      排查: pc\\logs\\ntfy_tunnel.log（fp.config 的 vps 块是否正确）")
        return False
    if code != 200:
        fail(f"health=HTTP {code}")
        return False
    ok("health=200（PC → SSH 隧道 → VPS ntfy 全链路通）")
    return True


# ---------------- 汇总 ----------------
def summary(results: list[tuple[str, bool]]) -> int:
    say("\n================ 安装结果 ================")
    all_ok = True
    for name, good in results:
        say(f"  {'[OK]' if good else '[!!]'} {name}")
        all_ok = all_ok and good
    say("")
    say("后续：")
    say("  1) 手机装 ntfy App，按 VPS 端 setup_vps.sh 打印的信息订阅 fp-phone")
    say("  2) 想改配置随时编辑 fp.config.json，改完重启：restart_services.bat")
    say("  3) 自检：.venv\\Scripts\\python.exe setup.py --check")
    say("==========================================")
    return 0 if all_ok else 1


# ---------------- --check 模式 ----------------
def do_check() -> int:
    say("== FxxkPush 环境自检（只读，不改动任何东西）==")
    say(f"项目根: {ROOT}")
    results: list[tuple[str, bool]] = []

    results.append(("项目标记文件", (ROOT / "pc" / "services" / "ai_triager.py").exists()))
    miss = deps_missing()
    results.append(("venv + 依赖" + (f"（缺 {','.join(miss)}）" if miss else ""), not miss))

    cfg_ok, detail = False, ""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        need = ("ai", "ntfy", "vps", "vision", "apps")
        cfg_ok = all(k in cfg for k in need)
        if cfg_ok:
            detail = (f"ai.base_url={'有' if cfg['ai'].get('base_url') else '空'} "
                      f"ntfy.token={'tk_..' if str(cfg['ntfy'].get('token','')).startswith('tk_') else '缺'} "
                      f"vps.host={cfg['vps'].get('host') or '缺'}")
        else:
            detail = f"缺块: {[k for k in need if k not in cfg]}"
    except Exception as e:                                  # noqa: BLE001
        detail = repr(e)
    results.append((f"fp.config.json（{detail}）", cfg_ok))

    good = 0
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if name in AUTOSTART.values():
                    parts = value.strip('"').split('" "')
                    if all(Path(p.replace("\\\\", "\\")).exists() for p in parts[:2]):
                        good += 1
    except Exception:                                       # noqa: BLE001
        pass
    results.append((f"开机自启 {good}/6（路径可达）", good == 6))

    try:
        with urllib.request.urlopen("http://127.0.0.1:2586/v1/health", timeout=5) as r:
            results.append((f"服务健康 health={r.status}", r.status == 200))
    except Exception as e:                                  # noqa: BLE001
        results.append((f"服务健康（{type(e).__name__}，未启动或隧道不通）", False))

    return summary(results)


# ---------------- main ----------------
def main() -> int:
    ap = argparse.ArgumentParser(description="FxxkPush 一键安装向导")
    ap.add_argument("--check", action="store_true", help="只检查，不改动")
    args = ap.parse_args()

    if args.check:
        return do_check()

    say("=" * 52)
    say("  FxxkPush 一键安装向导")
    say(f"  项目根: {ROOT}")
    say("=" * 52)
    results: list[tuple[str, bool]] = []
    results.append(("虚拟环境与依赖", step_deps()))
    results.append(("fp.config.json", step_config()))
    results.append(("通知授权", step_access()))
    results.append(("注册开机自启", step_autostart()))
    # 启动依赖配置正确；配置失败也继续尝试（可能保留了旧配置）
    results.append(("启动服务栈", step_start()))
    results.append(("窗口归位", step_park()))
    results.append(("健康验证", step_verify()))
    return summary(results)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n已中断")
        sys.exit(130)
