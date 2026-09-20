#!/usr/bin/env python3
"""Probe: can we access Windows notifications on this (ReviOS) machine?

Requests UserNotificationListener access and dumps current visible
notifications. Run once interactively; the Access request must be
approved via the toast/popup or in Settings > Privacy > Notifications.
"""
import asyncio
import sys

from winrt.windows.ui.notifications.management import (
    UserNotificationListener,
    UserNotificationListenerAccessStatus,
)
from winrt.windows.ui.notifications import (
    KnownNotificationBindings,
    NotificationKinds,
)


async def main():
    listener = UserNotificationListener.current
    access = await listener.request_access_async()
    print("access request result:", access)
    if access != UserNotificationListenerAccessStatus.ALLOWED:
        print("-> DENIED. Open 设置 > 隐私和安全性 > 通知 and allow desktop apps,"
              " then rerun. (Access request may only pop once per app identity.)")
        return 1

    notifs = await listener.get_notifications_async(NotificationKinds.TOAST)
    print(f"current notifications: {notifs.size}")
    for i in range(min(notifs.size, 10)):
        n = notifs.get_at(i)
        app = n.app_info.display_info.display_name
        try:
            text = n.notification.visual.get_binding(
                KnownNotificationBindings.toast_generic()).get_text_group()
            lines = [t.text for t in text]
        except Exception:
            lines = ["<no text binding>"]
        print(f"  [{i}] app={app!r} text={lines}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
