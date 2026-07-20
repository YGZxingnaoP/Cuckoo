# -*- coding: utf-8 -*-
"""
文件传输 Tab 页面
支持任意用户发送文件或文件夹，主机/房客均可选择目标。
文件夹传输保留目录结构。
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QFileDialog, QComboBox
)
from PySide6.QtCore import Qt, Signal


class FileTab(QWidget):
    """
    文件传输界面：选择文件/文件夹按钮、目标选择、进度条、速度/剩余时间。
    """

    # 信号: (file_path, target_id)
    file_send_requested = Signal(str, int)
    # 信号: (folder_path, target_id)
    folder_send_requested = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fileTab")
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 目标选择
        target_layout = QHBoxLayout()
        self._target_label = QLabel("发送给：")
        self._target_combo = QComboBox()
        self._target_combo.setObjectName("targetCombo")
        self._target_combo.setMinimumWidth(150)
        target_layout.addWidget(self._target_label)
        target_layout.addWidget(self._target_combo, stretch=1)
        layout.addLayout(target_layout)

        # 文件信息
        self._file_label = QLabel("未选择文件")
        self._file_label.setObjectName("fileLabel")
        layout.addWidget(self._file_label)

        # 进度条
        self._progress = QProgressBar()
        self._progress.setObjectName("fileProgress")
        self._progress.setMinimum(0)
        self._progress.setMaximum(100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        # 速度和剩余时间
        info_layout = QHBoxLayout()
        self._speed_label = QLabel("速度：--")
        self._speed_label.setObjectName("speedLabel")
        self._eta_label = QLabel("剩余：--")
        self._eta_label.setObjectName("etaLabel")
        info_layout.addWidget(self._speed_label)
        info_layout.addWidget(self._eta_label)
        layout.addLayout(info_layout)

        # 选择按钮行
        btn_layout = QHBoxLayout()
        self._btn_select = QPushButton("选择文件并发送")
        self._btn_select.setObjectName("btnSelectFile")
        self._btn_select.clicked.connect(self._on_select_file)
        btn_layout.addWidget(self._btn_select)

        self._btn_folder = QPushButton("选择文件夹并发送")
        self._btn_folder.setObjectName("btnSelectFolder")
        self._btn_folder.clicked.connect(self._on_select_folder)
        btn_layout.addWidget(self._btn_folder)
        layout.addLayout(btn_layout)

        layout.addStretch()

    # ─────────────────────────────────────────
    # 目标管理
    # ─────────────────────────────────────────

    def update_targets(self, targets: dict[int, str]) -> None:
        """更新可选目标列表：{uid: nickname}"""
        self._target_combo.clear()
        for uid, nick in targets.items():
            self._target_combo.addItem(f"{nick} (ID:{uid})", uid)

    def get_selected_target(self) -> int:
        """获取当前选中的目标 ID。"""
        idx = self._target_combo.currentIndex()
        if idx < 0:
            return -1
        return self._target_combo.itemData(idx)

    # ─────────────────────────────────────────
    # 事件
    # ─────────────────────────────────────────

    def _on_select_file(self) -> None:
        target_id = self.get_selected_target()
        if target_id < 0:
            self._file_label.setText("请先选择接收方")
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if path:
            self._file_label.setText(f"文件：{path}")
            self.file_send_requested.emit(path, target_id)

    def _on_select_folder(self) -> None:
        target_id = self.get_selected_target()
        if target_id < 0:
            self._file_label.setText("请先选择接收方")
            return
        path = QFileDialog.getExistingDirectory(self, "选择要发送的文件夹")
        if path:
            self._file_label.setText(f"文件夹：{path}")
            self.folder_send_requested.emit(path, target_id)

    # ─────────────────────────────────────────
    # 外部接口
    # ─────────────────────────────────────────

    def update_progress(self, percent: int, speed: str = "", eta: str = "") -> None:
        self._progress.setValue(percent)
        if speed:
            self._speed_label.setText(f"速度：{speed}")
        if eta:
            self._eta_label.setText(f"剩余：{eta}")

    def set_status(self, text: str) -> None:
        self._file_label.setText(text)
