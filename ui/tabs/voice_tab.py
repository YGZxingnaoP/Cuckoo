# -*- coding: utf-8 -*-
"""
语音通话 Tab 页面
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton,
    QSlider, QHBoxLayout, QGroupBox, QComboBox
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
    # 音频设备变更信号 (input_device_index, output_device_index)
    device_changed = Signal(int, int)

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
        self._status_label.setStyleSheet("font-size: 18px; color: #f0f0f0;")
        layout.addWidget(self._status_label)

        layout.addSpacing(20)

        # ── 音频设备选择 ──
        dev_group = QGroupBox("音频设备")
        dev_layout = QVBoxLayout(dev_group)

        # 输入设备（麦克风）
        in_row = QHBoxLayout()
        in_row.addWidget(QLabel("输入设备："))
        self._input_combo = QComboBox()
        self._input_combo.setObjectName("inputDeviceCombo")
        self._input_combo.setMinimumWidth(250)
        self._input_combo.currentIndexChanged.connect(self._on_device_changed)
        in_row.addWidget(self._input_combo, stretch=1)
        dev_layout.addLayout(in_row)

        # 输出设备（扬声器）
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("输出设备："))
        self._output_combo = QComboBox()
        self._output_combo.setObjectName("outputDeviceCombo")
        self._output_combo.setMinimumWidth(250)
        self._output_combo.currentIndexChanged.connect(self._on_device_changed)
        out_row.addWidget(self._output_combo, stretch=1)
        dev_layout.addLayout(out_row)

        layout.addWidget(dev_group, alignment=Qt.AlignCenter)

        layout.addSpacing(10)

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
        hint_label.setStyleSheet("color: #888888; font-size: 11px;")
        hint_label.setAlignment(Qt.AlignCenter)
        gain_layout.addWidget(hint_label)

        layout.addWidget(gain_group, alignment=Qt.AlignCenter)

    def _on_device_changed(self, index: int) -> None:
        in_idx = self._input_combo.currentData()
        out_idx = self._output_combo.currentData()
        if in_idx is not None and out_idx is not None:
            self.device_changed.emit(in_idx, out_idx)

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

    def set_devices(self, input_devices: list, output_devices: list) -> None:
        """设置可选设备列表。格式: [(name, device_index), ...]"""
        self._input_combo.blockSignals(True)
        self._output_combo.blockSignals(True)
        self._input_combo.clear()
        self._output_combo.clear()
        for name, idx in input_devices:
            self._input_combo.addItem(name, idx)
        for name, idx in output_devices:
            self._output_combo.addItem(name, idx)
        self._input_combo.blockSignals(False)
        self._output_combo.blockSignals(False)

    def get_selected_input(self) -> int:
        """获取选中的输入设备索引。"""
        data = self._input_combo.currentData()
        return data if data is not None else -1

    def get_selected_output(self) -> int:
        """获取选中的输出设备索引。"""
        data = self._output_combo.currentData()
        return data if data is not None else -1
