# -*- coding: utf-8 -*-
"""
文字聊天 Tab 页面
支持多人聊天，显示发送者昵称。
"""

from datetime import datetime
from typing import Optional, Callable

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton
)
from PySide6.QtCore import Qt, Signal

from core.protocol import NicknameRegistry


class ChatTab(QWidget):
    """
    文字聊天界面：消息展示区 + 输入框 + 发送按钮。
    支持多人聊天，根据 sender_id 显示不同昵称。
    """

    # 发送信号：(text) -> 由 MainWindow 连接到 Server/Client
    send_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatTab")
        self._my_id: int = 0
        self._nicknames: Optional[NicknameRegistry] = None
        self._init_ui()

    # ─────────────────────────────────────────
    # UI 构建
    # ─────────────────────────────────────────

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 消息展示区（只读）
        self._display = QTextEdit()
        self._display.setReadOnly(True)
        self._display.setObjectName("chatDisplay")
        layout.addWidget(self._display, stretch=1)

        # 输入区
        input_layout = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("输入消息...")
        self._input.setObjectName("chatInput")
        self._input.returnPressed.connect(self._on_send)

        self._btn_send = QPushButton("发送")
        self._btn_send.setObjectName("btnSend")
        self._btn_send.clicked.connect(self._on_send)

        input_layout.addWidget(self._input, stretch=1)
        input_layout.addWidget(self._btn_send)
        layout.addLayout(input_layout)

    # ─────────────────────────────────────────
    # 外部接口
    # ─────────────────────────────────────────

    def setup(self, my_id: int, nicknames: NicknameRegistry) -> None:
        """由 MainWindow 调用，设置身份信息。"""
        self._my_id = my_id
        self._nicknames = nicknames

    def append_message(self, sender_id: int, text: str) -> None:
        """显示一条接收到的消息。"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        name = self._nicknames.get(sender_id) if self._nicknames else f"用户{sender_id}"
        self._display.append(f"[{timestamp}] {name}: {text}")

    def append_system(self, text: str) -> None:
        """显示系统消息。"""
        self._display.append(f"[系统] {text}")

    # ─────────────────────────────────────────
    # 事件处理
    # ─────────────────────────────────────────

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self.send_requested.emit(text)
        # 显示自己的消息
        timestamp = datetime.now().strftime("%H:%M:%S")
        name = self._nicknames.get(self._my_id) if self._nicknames else "我"
        self._display.append(f"[{timestamp}] {name}: {text}")
        self._input.clear()
