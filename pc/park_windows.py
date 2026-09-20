#!/usr/bin/env python3
"""FxxkPush: park WeChat & WeCom windows off-screen (double-click helper).

Run after reboot / after reopening the apps, before vision listener's
next 30-min cycle, so the windows never sit visibly on screen.
Safe to run anytime: windows already parked are left alone.
Also correct for display / resolution / scaling changes.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wechat_vision_listener import (  # noqa: E402
    TARGETS, find_window, move_offscreen, park_needed,
)

DELAY = 3  # seconds to wait for slow splash screens
RETRY = 3  # attempts per app


def main():
    for attempt in range(RETRY):
        found_any = False
        for app, cfg in TARGETS.items():
            info = find_window(cfg["class"], cfg["title"], cfg["min_w"])
            if not info:
                print(f"[{app}] 窗口未找到（没开或托盘中），跳过")
                continue
            hwnd, x, y, w, h = info
            if park_needed(hwnd):
                move_offscreen(hwnd, w, h)
                print(f"[{app}] 已挪到屏幕外（显示器右边缘之外）")
            else:
                print(f"[{app}] 已在屏幕外，无需处理")
            found_any = True
        if found_any or attempt == RETRY - 1:
            break
        print(f"窗口还没就绪，{DELAY}s 后重试 ({attempt + 1}/{RETRY})...")
        time.sleep(DELAY)
    try:
        input("\n完成，按回车关闭...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
