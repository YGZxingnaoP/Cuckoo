# -*- coding: utf-8 -*-
"""
投屏 Tab 页面
主机显示控制按钮，房客显示接收画面。
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap


class ScreenTab(QWidget):
    """
    投屏界面：视频显示区域 + 开始/停止按钮（仅房主可见）。
    """

    # 房主投屏切换信号
    toggle_requested = Signal()

    def __init__(self, is_host: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("screenTab")
        self._is_host = is_host
        self._streaming = False
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 视频显示区域
        self._video_label = QLabel("等待投屏...")
        self._video_label.setObjectName("videoLabel")
        self._video_label.setAlignment(Qt.AlignCenter)
        self._video_label.setMinimumSize(640, 360)
        self._video_label.setStyleSheet("background-color: #1a1a1a; color: #888;")
        layout.addWidget(self._video_label, stretch=1)

        # 控制按钮（仅房主）
        if self._is_host:
            self._btn_toggle = QPushButton("开始投屏")
            self._btn_toggle.setObjectName("btnToggleScreen")
            self._btn_toggle.clicked.connect(self._on_toggle)
            layout.addWidget(self._btn_toggle)

    def _on_toggle(self) -> None:
        self.toggle_requested.emit()

    def start_streaming(self) -> None:
        """更新按钮状态为"正在投屏"。"""
        self._streaming = True
        if self._is_host:
            self._btn_toggle.setText("停止投屏")

    def stop_streaming(self) -> None:
        """更新按钮状态为"已停止"。"""
        self._streaming = False
        self._video_label.clear()
        self._video_label.setText("投屏已暂停")
        if self._is_host:
            self._btn_toggle.setText("开始投屏")

    def update_frame(self, pixmap: QPixmap) -> None:
        """接收端解码线程通过信号传入 QPixmap。"""
        self._video_label.setPixmap(
            pixmap.scaled(
                self._video_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
        )
