# -*- coding: utf-8 -*-
"""
语音通话 Tab 页面
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton
)
from PySide6.QtCore import Qt, Signal


class VoiceTab(QWidget):
    """
    语音通话界面：麦克风开关按钮 + 状态提示。
    """

    # 麦克风切换信号
    toggle_mic_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("voiceTab")
        self._mic_on = False
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        self._status_label = QLabel("语音通话")
        self._status_label.setObjectName("voiceStatusLabel")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setStyleSheet("font-size: 18px; color: #ccc;")
        layout.addWidget(self._status_label)

        layout.addSpacing(30)

        self._btn_mic = QPushButton("开启麦克风")
        self._btn_mic.setObjectName("btnMic")
        self._btn_mic.setFixedSize(200, 60)
        self._btn_mic.clicked.connect(self._on_toggle_mic)
        layout.addWidget(self._btn_mic, alignment=Qt.AlignCenter)

    def _on_toggle_mic(self) -> None:
        self.toggle_mic_requested.emit()

    def set_mic_on(self) -> None:
        self._mic_on = True
        self._btn_mic.setText("关闭麦克风")
        self._status_label.setText("麦克风已开启")

    def set_mic_off(self) -> None:
        self._mic_on = False
        self._btn_mic.setText("开启麦克风")
        self._status_label.setText("麦克风已关闭")

    @property
    def mic_on(self) -> bool:
        return self._mic_on
