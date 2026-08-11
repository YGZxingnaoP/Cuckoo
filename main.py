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
    background-color: #0a0a0a; color: #f0f0f0;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif; font-size: 13px;
}
QMainWindow { background-color: #0a0a0a; }
QGroupBox {
    border: 1px solid #2a2a2a; border-radius: 8px; margin-top: 12px; padding-top: 16px;
    font-weight: bold; color: #ffffff;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QPushButton {
    background-color: #141414; border: 1px solid #2a2a2a; border-radius: 6px;
    padding: 6px 16px; color: #f0f0f0; min-height: 24px;
}
QPushButton:hover { background-color: #2a2a2a; border: 1px solid #3a3a3a; }
QPushButton:pressed { background-color: #3a3a3a; }
QPushButton#btnConfirm {
    background-color: #f0f0f0; color: #0a0a0a; font-weight: bold; border: none;
}
QPushButton#btnConfirm:hover { background-color: #ffffff; }
QLineEdit, QComboBox {
    background-color: #141414; border: 1px solid #2a2a2a; border-radius: 6px;
    padding: 4px 8px; color: #f0f0f0; selection-background-color: #3a3a3a;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #f0f0f0; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background-color: #141414; border: 1px solid #2a2a2a;
    selection-background-color: #2a2a2a; color: #f0f0f0;
}
QRadioButton { spacing: 8px; color: #f0f0f0; }
QRadioButton::indicator { width: 16px; height: 16px; }

QTabWidget#mainTabs::tab-bar {
    left: 30px;
}

QTabWidget::pane { border: 1px solid #2a2a2a; border-radius: 6px; background-color: #0a0a0a; }
QTabBar::tab {
    background-color: #141414; border: 1px solid #2a2a2a; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
    padding: 8px 16px; margin-right: 4px; color: #888888;
}
QTabBar::tab:selected { background-color: #0a0a0a; color: #f0f0f0; border-bottom: 2px solid #f0f0f0; }
QTabBar::tab:hover { background-color: #2a2a2a; }
QListWidget { background-color: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 6px; outline: none; }
QListWidget::item { padding: 6px 12px; border-radius: 4px; margin: 2px 4px; }
QListWidget::item:selected { background-color: #2a2a2a; color: #f0f0f0; }
QListWidget::item:hover { background-color: #1a1a1a; }
QTextEdit { background-color: #0f0f0f; border: 1px solid #2a2a2a; border-radius: 6px; padding: 8px; color: #f0f0f0; }
QProgressBar {
    border: 1px solid #2a2a2a; border-radius: 6px; text-align: center;
    color: #0a0a0a; background-color: #141414; height: 18px;
}
QProgressBar::chunk { background-color: #f0f0f0; border-radius: 5px; }
QStatusBar { background-color: #0f0f0f; color: #888888; border-top: 1px solid #2a2a2a; }
QCheckBox { spacing: 8px; color: #f0f0f0; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #2a2a2a; border-radius: 4px; background-color: #141414;
}
QCheckBox::indicator:checked { background-color: #f0f0f0; border: 1px solid #f0f0f0; }
QSlider::groove:horizontal { border: 1px solid #2a2a2a; height: 6px; background: #141414; border-radius: 3px; }
QSlider::handle:horizontal {
    background: #f0f0f0; border: 1px solid #f0f0f0; width: 16px; margin: -6px 0; border-radius: 9px;
}
QSlider::handle:horizontal:hover { background: #ffffff; border: 1px solid #ffffff; }
"""


def _init_vlc_path() -> None:
    """
    初始化 VLC 运行时路径，确保打包后也能找到 libvlc.dll 和 plugins。
    开发模式：runtime/ 目录下的 VLC 文件
    打包模式：sys._MEIPASS/vlc/ 下的 VLC 文件
    """
    if getattr(sys, 'frozen', False):
        vlc_dir = os.path.join(sys._MEIPASS, "vlc")
    else:
        vlc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")

    vlc_plugins = os.path.join(vlc_dir, "plugins")

    if not os.path.isdir(vlc_plugins):
        return  # VLC 未打包，回退到系统安装的 VLC

    # 1. add_dll_directory (Python 3.8+ / Windows 10 1809+)
    try:
        os.add_dll_directory(vlc_dir)
    except AttributeError:
        pass

    # 2. PATH 兜底（兼容所有 Windows 版本）
    os.environ["PATH"] = vlc_dir + os.pathsep + os.environ.get("PATH", "")

    # 3. VLC 插件路径
    os.environ["VLC_PLUGIN_PATH"] = vlc_plugins


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Cuckoo")
    app.setStyle("Fusion")

    # ── VLC 运行时路径初始化 ──
    _init_vlc_path()

    # ── 确保必要目录存在 ──
    import config
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(config.MOVIES_DIR, exist_ok=True)

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
