# -*- coding: utf-8 -*-
"""
Cuckoo 私有实时通信平台 —— 程序入口
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt

from ui.start_dialog import StartDialog
from ui.main_window import MainWindow
from common import logger as log

# ─────────────────────────────────────────────
# 全局深色主题样式 (Catppuccin Mocha)
# ─────────────────────────────────────────────
GLOBAL_QSS = """
QWidget {
    background-color: #1e1e2e; color: #cdd6f4;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 13px;
}
QMainWindow { background-color: #1e1e2e; }
QGroupBox {
    border: 1px solid #45475a; border-radius: 8px; margin-top: 12px; padding-top: 16px;
    font-weight: bold; color: #89b4fa;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QPushButton {
    background-color: #313244; border: 1px solid #45475a; border-radius: 6px;
    padding: 6px 16px; color: #cdd6f4; min-height: 24px;
}
QPushButton:hover { background-color: #45475a; border: 1px solid #585b70; }
QPushButton:pressed { background-color: #585b70; }
QPushButton#btnConfirm {
    background-color: #89b4fa; color: #1e1e2e; font-weight: bold; border: none;
}
QPushButton#btnConfirm:hover { background-color: #b4befe; }
QLineEdit, QComboBox {
    background-color: #313244; border: 1px solid #45475a; border-radius: 6px;
    padding: 4px 8px; color: #cdd6f4; selection-background-color: #89b4fa;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #89b4fa; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background-color: #313244; border: 1px solid #45475a;
    selection-background-color: #45475a; color: #cdd6f4;
}
QRadioButton { spacing: 8px; color: #cdd6f4; }
QRadioButton::indicator { width: 16px; height: 16px; }
QTabWidget::pane { border: 1px solid #45475a; border-radius: 6px; background-color: #1e1e2e; }
QTabBar::tab {
    background-color: #313244; border: 1px solid #45475a; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    padding: 8px 16px; margin-right: 4px; color: #a6adc8;
}
QTabBar::tab:selected { background-color: #1e1e2e; color: #89b4fa; border-bottom: 2px solid #89b4fa; }
QTabBar::tab:hover { background-color: #45475a; }
QListWidget { background-color: #181825; border: 1px solid #45475a; border-radius: 6px; outline: none; }
QListWidget::item { padding: 6px 12px; border-radius: 4px; margin: 2px 4px; }
QListWidget::item:selected { background-color: #45475a; color: #89b4fa; }
QListWidget::item:hover { background-color: #313244; }
QTextEdit { background-color: #181825; border: 1px solid #45475a; border-radius: 6px; padding: 8px; color: #cdd6f4; }
QProgressBar {
    border: 1px solid #45475a; border-radius: 6px; text-align: center;
    color: #1e1e2e; background-color: #313244; height: 18px;
}
QProgressBar::chunk { background-color: #89b4fa; border-radius: 5px; }
QStatusBar { background-color: #181825; color: #a6adc8; border-top: 1px solid #313244; }
QCheckBox { spacing: 8px; color: #cdd6f4; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #45475a; border-radius: 4px; background-color: #313244;
}
QCheckBox::indicator:checked { background-color: #89b4fa; border: 1px solid #89b4fa; }
QSlider::groove:horizontal { border: 1px solid #45475a; height: 6px; background: #313244; border-radius: 3px; }
QSlider::handle:horizontal {
    background: #89b4fa; border: 1px solid #89b4fa; width: 16px; margin: -6px 0; border-radius: 9px;
}
QSlider::handle:horizontal:hover { background: #b4befe; border: 1px solid #b4befe; }
"""


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Cuckoo")
    app.setStyle("Fusion")

    # ── 设置程序图标 ──
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # ── 应用全局深色主题 ──
    app.setStyleSheet(GLOBAL_QSS)

    log.log("Main", "Application starting (Star Topology)...")

    while True:
        dialog = StartDialog()
        if dialog.exec() != StartDialog.Accepted:
            log.log("Main", "User cancelled start dialog")
            sys.exit(0)

        is_host = dialog.is_host
        peer_ip = dialog.peer_ip
        nickname = dialog.nickname

        log.log("Main", f"Role: {'Host' if is_host else 'Guest'}, PeerIP: {peer_ip}, Nickname: {nickname}")

        window = MainWindow(is_host=is_host, peer_ip=peer_ip, nickname=nickname)
        window.show()

        if not is_host:
            disconnected = [False]
            def _on_host_disconnected(): disconnected[0] = True
            window.host_disconnected.connect(_on_host_disconnected)

        app.exec()

        if not is_host and disconnected[0]:
            log.log("Main", "Host disconnected, returning to start dialog")
            window.deleteLater()
            continue
        else:
            break

    sys.exit(0)

if __name__ == "__main__":
    main()
