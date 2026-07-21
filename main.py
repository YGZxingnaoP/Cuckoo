# -*- coding: utf-8 -*-
"""
Cuckoo 私有实时通信平台 —— 程序入口
星型拓扑（Star Topology）架构
运行方式：runtime/python.exe main.py
"""

import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from ui.start_dialog import StartDialog
from ui.main_window import MainWindow
from common import logger as log


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Cuckoo")
    app.setStyle("Fusion")

    log.log("Main", "Application starting (Star Topology)...")

    while True:
        # ── 启动对话框 ──
        dialog = StartDialog()
        if dialog.exec() != StartDialog.Accepted:
            log.log("Main", "User cancelled start dialog")
            sys.exit(0)

        is_host = dialog.is_host
        peer_ip = dialog.peer_ip
        nickname = dialog.nickname

        log.log("Main", f"Role: {'Host' if is_host else 'Guest'}, "
                        f"PeerIP: {peer_ip}, Nickname: {nickname}")

        # ── 创建主窗口 ──
        window = MainWindow(is_host=is_host, peer_ip=peer_ip, nickname=nickname)
        window.show()

        # 房客模式下：房主断开时关闭窗口并跳回启动界面
        if not is_host:
            disconnected = [False]

            def _on_host_disconnected():
                disconnected[0] = True

            window.host_disconnected.connect(_on_host_disconnected)

        app.exec()

        # 如果是房主断开导致的退出，继续循环重新显示启动对话框
        if not is_host and disconnected[0]:
            log.log("Main", "Host disconnected, returning to start dialog")
            window.deleteLater()
            continue
        else:
            break

    sys.exit(0)


if __name__ == "__main__":
    main()
