# -*- coding: utf-8 -*-
"""
文件传输 Tab 页面 (卡片式块状布局)
"""

import os
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QFileDialog, QComboBox,
    QGroupBox, QListWidget, QListWidgetItem, QGridLayout, QFrame
)
from PySide6.QtCore import Qt, Signal


class FileTab(QWidget):
    file_send_requested = Signal(str, int)
    folder_send_requested = Signal(str, int)
    resume_requested = Signal(str)
    clear_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fileTab")
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # ── 1. 目标选择区 (卡片) ──
        target_frame = QFrame()
        target_frame.setObjectName("targetFrame")
        target_frame.setStyleSheet("""
            QFrame#targetFrame {
                background-color: #313244;
                border: 1px solid #45475a;
                border-radius: 8px;
                padding: 12px;
            }
        """)
        target_layout = QHBoxLayout(target_frame)
        target_layout.setContentsMargins(12, 8, 12, 8)
        
        self._target_label = QLabel("发送给：")
        self._target_label.setStyleSheet("color: #a6adc8; font-weight: bold; background: transparent; border: none;")
        target_layout.addWidget(self._target_label)
        
        self._target_combo = QComboBox()
        self._target_combo.setMinimumWidth(200)
        self._target_combo.setStyleSheet("background-color: #1e1e2e; border: 1px solid #45475a; border-radius: 4px; padding: 4px;")
        target_layout.addWidget(self._target_combo, stretch=1)
        layout.addWidget(target_frame)

        # ── 2. 状态与进度区 (卡片) ──
        status_frame = QFrame()
        status_frame.setObjectName("statusFrame")
        status_frame.setStyleSheet("""
            QFrame#statusFrame {
                background-color: #181825;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 12px;
            }
        """)
        status_layout = QVBoxLayout(status_frame)
        status_layout.setSpacing(8)
        
        self._file_label = QLabel("未选择文件")
        self._file_label.setStyleSheet("color: #cdd6f4; font-size: 13px; background: transparent; border: none;")
        self._file_label.setWordWrap(True)
        status_layout.addWidget(self._file_label)
        
        self._progress = QProgressBar()
        self._progress.setMinimum(0)
        self._progress.setMaximum(100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(12)
        self._progress.setTextVisible(False)
        status_layout.addWidget(self._progress)
        
        info_layout = QHBoxLayout()
        self._speed_label = QLabel("速度：--")
        self._speed_label.setStyleSheet("color: #a6adc8; font-size: 12px; background: transparent; border: none;")
        self._eta_label = QLabel("剩余：--")
        self._eta_label.setStyleSheet("color: #a6adc8; font-size: 12px; background: transparent; border: none;")
        info_layout.addWidget(self._speed_label)
        info_layout.addStretch()
        info_layout.addWidget(self._eta_label)
        status_layout.addLayout(info_layout)
        
        layout.addWidget(status_frame)

        # ── 3. 操作按钮区 (块状按钮，告别长条) ──
        btn_layout = QGridLayout()
        btn_layout.setSpacing(12)
        
        self._btn_select = QPushButton("📄  选择文件并发送")
        self._btn_select.setObjectName("btnAction")
        self._btn_select.setMinimumHeight(48)
        self._btn_select.setStyleSheet("""
            QPushButton#btnAction {
                background-color: #89b4fa;
                color: #1e1e2e;
                font-weight: bold;
                font-size: 14px;
                border: none;
                border-radius: 8px;
                padding: 10px;
            }
            QPushButton#btnAction:hover { background-color: #b4befe; }
            QPushButton#btnAction:pressed { background-color: #74c7ec; }
        """)
        self._btn_select.clicked.connect(self._on_select_file)
        btn_layout.addWidget(self._btn_select, 0, 0)

        self._btn_folder = QPushButton("📁  选择文件夹并发送")
        self._btn_folder.setObjectName("btnActionSecondary")
        self._btn_folder.setMinimumHeight(48)
        self._btn_folder.setStyleSheet("""
            QPushButton#btnActionSecondary {
                background-color: #45475a;
                color: #cdd6f4;
                font-weight: bold;
                font-size: 14px;
                border: 1px solid #585b70;
                border-radius: 8px;
                padding: 10px;
            }
            QPushButton#btnActionSecondary:hover { background-color: #585b70; border: 1px solid #6c7086; }
            QPushButton#btnActionSecondary:pressed { background-color: #313244; }
        """)
        self._btn_folder.clicked.connect(self._on_select_folder)
        btn_layout.addWidget(self._btn_folder, 0, 1)
        
        layout.addLayout(btn_layout)

        # ── 4. 断点续传区 ──
        self._interrupt_group = QGroupBox("⏸ 中断的传输任务 (断点续传)")
        self._interrupt_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                color: #f9e2af;
                border: 1px solid #45475a;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 16px;
                background-color: #181825;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
        """)
        int_layout = QVBoxLayout(self._interrupt_group)
        int_layout.setSpacing(8)
        
        self._interrupt_list = QListWidget()
        self._interrupt_list.setMaximumHeight(120)
        self._interrupt_list.setStyleSheet("""
            QListWidget {
                background-color: #1e1e2e;
                border: 1px solid #313244;
                border-radius: 6px;
                color: #cdd6f4;
                outline: none;
            }
            QListWidget::item { padding: 6px 10px; border-radius: 4px; }
            QListWidget::item:selected { background-color: #45475a; color: #f9e2af; }
        """)
        int_layout.addWidget(self._interrupt_list)
        
        int_btn_layout = QHBoxLayout()
        int_btn_layout.setSpacing(10)
        
        self._btn_resume = QPushButton("▶ 继续传输")
        self._btn_resume.setMinimumHeight(36)
        self._btn_resume.setStyleSheet("""
            QPushButton {
                background-color: #a6e3a1; color: #1e1e2e; font-weight: bold;
                border: none; border-radius: 6px; padding: 6px 16px;
            }
            QPushButton:hover { background-color: #94e2d5; }
        """)
        self._btn_resume.clicked.connect(self._on_resume)
        int_btn_layout.addWidget(self._btn_resume)
        
        self._btn_clear = QPushButton("✕ 清除任务")
        self._btn_clear.setMinimumHeight(36)
        self._btn_clear.setStyleSheet("""
            QPushButton {
                background-color: #f38ba8; color: #1e1e2e; font-weight: bold;
                border: none; border-radius: 6px; padding: 6px 16px;
            }
            QPushButton:hover { background-color: #eba0ac; }
        """)
        self._btn_clear.clicked.connect(self._on_clear)
        int_btn_layout.addWidget(self._btn_clear)
        
        int_btn_layout.addStretch()
        int_layout.addLayout(int_btn_layout)
        
        layout.addWidget(self._interrupt_group)
        layout.addStretch()

    def update_targets(self, targets: dict[int, str]) -> None:
        self._target_combo.clear()
        for uid, nick in targets.items():
            self._target_combo.addItem(f"{nick} (ID:{uid})", uid)

    def get_selected_target(self) -> int:
        idx = self._target_combo.currentIndex()
        if idx < 0:
            return -1
        data = self._target_combo.itemData(idx)
        return data if data is not None else -1

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

    def update_progress(self, task_id: str, percent: int, speed: str = "", eta: str = "") -> None:
        self._progress.setValue(percent)
        if speed: self._speed_label.setText(f"速度：{speed}")
        if eta: self._eta_label.setText(f"剩余：{eta}")

    def set_status(self, text: str) -> None:
        self._file_label.setText(text)

    def add_interrupted_task(self, task_id: str, display_name: str):
        for i in range(self._interrupt_list.count()):
            if self._interrupt_list.item(i).data(Qt.UserRole) == task_id:
                return
        item = QListWidgetItem(display_name)
        item.setData(Qt.UserRole, task_id)
        self._interrupt_list.addItem(item)

    def remove_interrupted_task(self, task_id: str):
        for i in range(self._interrupt_list.count()):
            if self._interrupt_list.item(i).data(Qt.UserRole) == task_id:
                self._interrupt_list.takeItem(i)
                break

    def _on_resume(self):
        item = self._interrupt_list.currentItem()
        if item:
            self.resume_requested.emit(item.data(Qt.UserRole))

    def _on_clear(self):
        item = self._interrupt_list.currentItem()
        if item:
            self.clear_requested.emit(item.data(Qt.UserRole))
            self._interrupt_list.takeItem(self._interrupt_list.row(item))
