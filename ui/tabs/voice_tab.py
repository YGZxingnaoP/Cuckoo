# -*- coding: utf-8 -*-
"""
语音通话 Tab 页面
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton,
    QSlider, QHBoxLayout, QGroupBox
)
from PySide6.QtCore import Qt, Signal


class VoiceTab(QWidget):
    """
    语音通话界面：麦克风开关按钮 + 音量增幅滑块 + 状态提示。
    """

    # 麦克风切换信号
    toggle_mic_requested = Signal()
    # 音量增幅变更信号 (gain_percent: 50~300, 100=原始)
    volume_gain_changed = Signal(int)

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

        layout.addSpacing(30)

        # ── 音量增幅控制 ──
        gain_group = QGroupBox("音量增幅（对方声音大小）")
        gain_layout = QVBoxLayout(gain_group)

        slider_row = QHBoxLayout()
        self._gain_label = QLabel("100%")
        self._gain_label.setFixedWidth(50)
        self._gain_label.setAlignment(Qt.AlignCenter)

        self._gain_slider = QSlider(Qt.Horizontal)
        self._gain_slider.setRange(50, 300)
        self._gain_slider.setValue(100)
        self._gain_slider.setTickPosition(QSlider.TicksBelow)
        self._gain_slider.setTickInterval(50)
        self._gain_slider.setFixedWidth(300)
        self._gain_slider.valueChanged.connect(self._on_gain_changed)

        slider_row.addWidget(self._gain_slider)
        slider_row.addWidget(self._gain_label)
        gain_layout.addLayout(slider_row)

        hint_label = QLabel("拖动滑块调节：50% 降低音量，100% 原始，300% 最大增幅")
        hint_label.setStyleSheet("color: #999; font-size: 11px;")
        hint_label.setAlignment(Qt.AlignCenter)
        gain_layout.addWidget(hint_label)

        layout.addWidget(gain_group, alignment=Qt.AlignCenter)

    def _on_toggle_mic(self) -> None:
        self.toggle_mic_requested.emit()

    def _on_gain_changed(self, value: int) -> None:
        self._gain_label.setText(f"{value}%")
        self.volume_gain_changed.emit(value)

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

    @property
    def gain_percent(self) -> int:
        return self._gain_slider.value()
