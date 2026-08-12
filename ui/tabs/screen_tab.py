# -*- coding: utf-8 -*-
"""
投屏 Tab 页面
主机显示控制按钮、分辨率选择和帧率选择，房客显示接收画面。
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QGroupBox, QFrame, QCheckBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QImage

import config


class ScreenTab(QWidget):
    """
    投屏界面：视频显示区域 + 开始/停止按钮 + 分辨率/帧率选择（仅房主可见）。
    """

    # 房主投屏切换信号
    toggle_requested = Signal()
    # 分辨率变更信号: (width, height) — (0,0) 表示原画
    resolution_changed = Signal(int, int)
    # 帧率变更信号: fps
    fps_changed = Signal(int)
    # 扬声器变更信号 (speaker_name) — 用于系统音频采集
    speaker_changed = Signal(str)
    # 共享电脑声音开关变更信号 (enabled: bool)
    share_audio_toggled = Signal(bool)

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
        self._video_label.setStyleSheet("background-color: #0f0f0f; color: #888888; border: 1px solid #2a2a2a; border-radius: 6px;")
        layout.addWidget(self._video_label, stretch=1)

        # 控制区域（仅房主）
        if self._is_host:
            # ── 折叠控制按钮 ──
            collapse_row = QHBoxLayout()
            collapse_row.setAlignment(Qt.AlignRight)
            self._btn_config_toggle = QPushButton("▼ 投屏配置")
            self._btn_config_toggle.setObjectName("btnConfigToggle")
            self._btn_config_toggle.setFixedWidth(120)
            self._btn_config_toggle.setStyleSheet(
                "QPushButton { font-size: 11px; color: #888; background: transparent; "
                "border: 1px solid #2a2a2a; border-radius: 3px; padding: 2px 6px; }"
                "QPushButton:hover { color: #f0f0f0; border-color: #888888; }"
            )
            self._btn_config_toggle.clicked.connect(self._toggle_config_visible)
            collapse_row.addWidget(self._btn_config_toggle)
            layout.addLayout(collapse_row)

            # ── 可折叠配置容器 ──
            self._config_widget = QWidget()
            config_layout = QVBoxLayout(self._config_widget)
            config_layout.setContentsMargins(0, 4, 0, 0)

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

            # 帧率选择
            ctrl_layout.addWidget(QLabel("帧率："))
            self._fps_combo = QComboBox()
            self._fps_combo.setObjectName("fpsCombo")
            for name, fps in config.FPS_PRESETS:
                self._fps_combo.addItem(name, fps)
            self._fps_combo.setCurrentIndex(config.DEFAULT_FPS_PRESET)
            self._fps_combo.currentIndexChanged.connect(self._on_fps_changed)
            ctrl_layout.addWidget(self._fps_combo)

            # 投屏开关按钮
            self._btn_toggle = QPushButton("开始投屏")
            self._btn_toggle.setObjectName("btnToggleScreen")
            self._btn_toggle.clicked.connect(self._on_toggle)
            ctrl_layout.addWidget(self._btn_toggle)

            config_layout.addLayout(ctrl_layout)

            # ── 共享电脑声音配置（房主端） ──
            share_group = QGroupBox("共享电脑声音")
            share_layout = QVBoxLayout(share_group)

            # 开关复选框
            self._share_checkbox = QCheckBox("启用共享电脑声音（排除麦克风）")
            self._share_checkbox.setChecked(False)
            self._share_checkbox.toggled.connect(self._on_share_toggled)
            share_layout.addWidget(self._share_checkbox)

            # 扬声器选择（用于 WASAPI loopback 采集）
            spk_row = QHBoxLayout()
            spk_row.addWidget(QLabel("采集扬声器："))
            self._speaker_combo = QComboBox()
            self._speaker_combo.setObjectName("speakerCombo")
            self._speaker_combo.setMinimumWidth(200)
            self._speaker_combo.currentIndexChanged.connect(self._on_speaker_changed)
            spk_row.addWidget(self._speaker_combo, stretch=1)
            share_layout.addLayout(spk_row)

            hint = QLabel("选择要采集的扬声器设备，仅该扬声器的声音会共享给房客")
            hint.setStyleSheet("color: #888888; font-size: 10px;")
            hint.setWordWrap(True)
            share_layout.addWidget(hint)

            config_layout.addWidget(share_group)

            layout.addWidget(self._config_widget)
            self._config_visible = True

    def _on_toggle(self) -> None:
        self.toggle_requested.emit()

    def _toggle_config_visible(self) -> None:
        """切换投屏配置区域的可见性。"""
        self._config_visible = not self._config_visible
        self._config_widget.setVisible(self._config_visible)
        self._btn_config_toggle.setText("▼ 投屏配置" if self._config_visible else "▶ 投屏配置")

    def _on_res_changed(self, index: int) -> None:
        data = self._res_combo.itemData(index)
        if data:
            w, h = data
            self.resolution_changed.emit(w, h)

    def _on_fps_changed(self, index: int) -> None:
        fps = self._fps_combo.itemData(index)
        if fps:
            self.fps_changed.emit(fps)

    def _on_share_toggled(self, checked: bool) -> None:
        self.share_audio_toggled.emit(checked)

    def _on_speaker_changed(self, index: int) -> None:
        name = self._speaker_combo.currentData()
        if name is not None:
            self.speaker_changed.emit(name)

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

    def update_frame(self, image: QImage) -> None:
        """接收端解码线程通过信号传入 QImage，在主线程转换为 QPixmap。"""
        pixmap = QPixmap.fromImage(image)
        label_size = self._video_label.size()
        # 仅在尺寸不匹配时才缩放，避免无意义的二次滤波导致模糊
        if abs(pixmap.width() - label_size.width()) > 2 or abs(pixmap.height() - label_size.height()) > 2:
            pixmap = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._video_label.setPixmap(pixmap)

    def set_speakers(self, speakers: list) -> None:
        """设置可选扬声器列表。格式: [(name, name), ...]"""
        if not self._is_host:
            return
        if hasattr(self, '_speaker_combo'):
            self._speaker_combo.blockSignals(True)
            self._speaker_combo.clear()
            for name, val in speakers:
                self._speaker_combo.addItem(name, val)
            self._speaker_combo.blockSignals(False)

    def is_share_audio_enabled(self) -> bool:
        """是否启用了共享电脑声音。"""
        return hasattr(self, '_share_checkbox') and self._share_checkbox.isChecked()

    def get_selected_speaker(self) -> str:
        """获取选中的扬声器名称。"""
        if hasattr(self, '_speaker_combo'):
            data = self._speaker_combo.currentData()
            return data if data is not None else ""
        return ""
