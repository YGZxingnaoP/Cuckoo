# -*- coding: utf-8 -*-
"""
在线用户列表面板（可完全折叠）
独立显示在侧边栏，展示昵称和当前模式。
折叠时宽度为 0（完全隐藏），通过主窗口的浮动按钮展开。
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QHBoxLayout, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, Signal


class OnlineListPanel(QWidget):
    """
    可完全折叠的在线用户列表面板。
    - 展开：180px，显示模式标签 + 用户列表 + 右上角折叠按钮
    - 收起：0px，完全隐藏
    """

    EXPANDED_WIDTH = 180
    COLLAPSED_WIDTH = 0

    # 折叠状态变更信号
    collapsed_changed = Signal(bool)  # True=已折叠

    def __init__(self, is_host: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("onlineListPanel")
        self._is_host = is_host
        self._collapsed = False
        self._latest_users: dict = {}  # 缓存最新用户数据
        self._init_ui()
        # 初始为展开状态
        self.setFixedWidth(self.EXPANDED_WIDTH)

    def _init_ui(self) -> None:
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(4, 4, 4, 4)
        self._main_layout.setSpacing(6)

        # ── 顶部栏：折叠按钮在右上角 ──
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(2)

        self._header_label = QLabel("在线")
        self._header_label.setStyleSheet("font-size: 11px; color: #aaa; font-weight: bold;")
        top_row.addWidget(self._header_label, stretch=1)

        # 右上角折叠按钮（隐蔽风格）
        self._btn_toggle = QPushButton("✕")
        self._btn_toggle.setFixedSize(20, 20)
        self._btn_toggle.setStyleSheet(
            "QPushButton { font-size: 9px; padding: 0; border: none; "
            "background: transparent; color: #666; }"
            "QPushButton:hover { color: #ccc; background: #333; border-radius: 3px; }"
        )
        self._btn_toggle.setToolTip("收起在线列表")
        self._btn_toggle.clicked.connect(self.toggle_collapse)
        top_row.addWidget(self._btn_toggle)

        self._main_layout.addLayout(top_row)

        # ── 模式标签 ──
        role_text = "房主模式" if self._is_host else "房客模式"
        role_color = "#4a4" if self._is_host else "#48f"
        self._role_label = QLabel(role_text)
        self._role_label.setAlignment(Qt.AlignCenter)
        self._role_label.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {role_color}; "
            f"padding: 4px; background: #222; border-radius: 4px;"
        )
        self._main_layout.addWidget(self._role_label)

        # ── 在线列表标题 ──
        self._title_label = QLabel("在线用户")
        self._title_label.setStyleSheet("font-size: 11px; color: #999;")
        self._title_label.setAlignment(Qt.AlignCenter)
        self._main_layout.addWidget(self._title_label)

        # ── 用户列表 ──
        self._user_list = QListWidget()
        self._user_list.setObjectName("onlineUserList")
        self._user_list.setStyleSheet(
            "QListWidget { font-size: 13px; } "
            "QListWidget::item { padding: 4px 8px; }"
        )
        self._main_layout.addWidget(self._user_list, stretch=1)

    # ═════════════════════════════════════════
    # 展开/收起
    # ═════════════════════════════════════════

    def toggle_collapse(self) -> None:
        """切换展开/收起状态。"""
        if self._collapsed:
            self._expand()
        else:
            self._collapse()

    def _collapse(self) -> None:
        """完全折叠面板（隐藏）。"""
        self._collapsed = True
        self.setVisible(False)
        self.setFixedWidth(self.COLLAPSED_WIDTH)
        self.collapsed_changed.emit(True)

    def _expand(self) -> None:
        """展开面板。"""
        self._collapsed = False
        self.setFixedWidth(self.EXPANDED_WIDTH)
        self.setVisible(True)
        self.collapsed_changed.emit(False)

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed

    # ═════════════════════════════════════════
    # 数据更新
    # ═════════════════════════════════════════

    def update_users(self, users: dict) -> None:
        """
        更新在线用户列表。
        :param users: {uid: nickname}
        """
        self._latest_users = dict(users)
        self._refresh_display()

    def refresh(self) -> None:
        """强制刷新显示（使用缓存数据）。"""
        self._refresh_display()

    def _refresh_display(self) -> None:
        """重新渲染用户列表。"""
        self._user_list.clear()
        for uid, nick in sorted(self._latest_users.items(), key=lambda x: x[0]):
            item = QListWidgetItem(nick)
            self._user_list.addItem(item)
        # 强制重绘确保可见
        self._user_list.viewport().update()

    def set_role(self, is_host: bool) -> None:
        """更新角色显示。"""
        self._is_host = is_host
        role_text = "房主模式" if is_host else "房客模式"
        role_color = "#4a4" if is_host else "#48f"
        self._role_label.setText(role_text)
        self._role_label.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {role_color}; "
            f"padding: 4px; background: #222; border-radius: 4px;"
        )
