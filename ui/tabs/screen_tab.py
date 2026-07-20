# -*- coding: utf-8 -*-
"""
投屏 Tab 页面
主机显示控制按钮和分辨率选择，房客显示接收画面。
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap

import config


class ScreenTab(QWidget):
    """
    投屏界面：视频显示区域 + 开始/停止按钮 + 分辨率选择（仅房主可见）。
    """

    # 房主投屏切换信号
    toggle_requested = Signal()
    # 分辨率变更信号: (width, height)  — (0,0) 表示原画
    resolution_changed = Signal(int, int)

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

        # 控制区域（仅房主）
        if self._is_host:
            ctrl_layout = QHBoxLayout()

            # 分辨率选择
            ctrl_layout.addWidget(QLabel("分辨率："))
            self._res_combo = QComboBox()
            self._res_combo.setObjectName("resCombo")
            for name, w, h in config.SCREEN_PRESETS:
                self._res_combo.addItem(name, (w, h))
            self._res_combo.setCurrentIndex(config.DEFAULT_SCREEN_PRESET)
            self._res_combo.currentIndexChanged.connect(self._on_res_changed)
            ctrl_layout.addWidget(self._res_combo)

            # 投屏开关按钮
            self._btn_toggle = QPushButton("开始投屏")
            self._btn_toggle.setObjectName("btnToggleScreen")
            self._btn_toggle.clicked.connect(self._on_toggle)
            ctrl_layout.addWidget(self._btn_toggle)

            layout.addLayout(ctrl_layout)

    def _on_toggle(self) -> None:
        self.toggle_requested.emit()

    def _on_res_changed(self, index: int) -> None:
        data = self._res_combo.itemData(index)
        if data:
            w, h = data
            self.resolution_changed.emit(w, h)

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
