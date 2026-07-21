# -*- coding: utf-8 -*-
"""
在线用户列表面板（可展开/收起）
独立显示在侧边栏，展示昵称和当前模式。
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QHBoxLayout
)
from PySide6.QtCore import Qt


class OnlineListPanel(QWidget):
    """
    可展开/收起的在线用户列表面板。
    - 展开：180px，显示模式标签 + 用户列表
    - 收起：32px，仅显示竖排文字和展开按钮
    """

    EXPANDED_WIDTH = 180
    COLLAPSED_WIDTH = 32

    def __init__(self, is_host: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("onlineListPanel")
        self.setMinimumWidth(self.COLLAPSED_WIDTH)
        self.setMaximumWidth(self.EXPANDED_WIDTH)
        self._is_host = is_host
        self._collapsed = False
        self._init_ui()
        # 初始为展开状态
        self.setFixedWidth(self.EXPANDED_WIDTH)

    def _init_ui(self) -> None:
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(4, 4, 4, 4)
        self._main_layout.setSpacing(6)

        # ── 顶部栏：收起/展开按钮 ──
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(2)

        self._btn_toggle = QPushButton("◀")
        self._btn_toggle.setFixedSize(24, 24)
        self._btn_toggle.setStyleSheet(
            "QPushButton { font-size: 10px; padding: 0; border: none; "
            "background: #333; color: #ccc; border-radius: 3px; }"
            "QPushButton:hover { background: #555; }"
        )
        self._btn_toggle.clicked.connect(self.toggle_collapse)
        top_row.addWidget(self._btn_toggle)

        self._header_label = QLabel("在线")
        self._header_label.setStyleSheet("font-size: 11px; color: #aaa; font-weight: bold;")
        top_row.addWidget(self._header_label, stretch=1)

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
        """收起面板。"""
        self._collapsed = True
        self.setFixedWidth(self.COLLAPSED_WIDTH)
        self._btn_toggle.setText("▶")
        # 隐藏内容控件
        self._role_label.setVisible(False)
        self._title_label.setVisible(False)
        self._user_list.setVisible(False)
        self._header_label.setVisible(False)
        # 设置工具提示
        self.setToolTip("点击展开在线列表")

    def _expand(self) -> None:
        """展开面板。"""
        self._collapsed = False
        self.setFixedWidth(self.EXPANDED_WIDTH)
        self._btn_toggle.setText("◀")
        # 显示内容控件
        self._role_label.setVisible(True)
        self._title_label.setVisible(True)
        self._user_list.setVisible(True)
        self._header_label.setVisible(True)
        self.setToolTip("")

    # ═════════════════════════════════════════
    # 数据更新
    # ═════════════════════════════════════════

    def update_users(self, users: dict) -> None:
        """
        更新在线用户列表。
        :param users: {uid: nickname}
        """
        self._user_list.clear()
        for uid, nick in sorted(users.items(), key=lambda x: x[0]):
            item = QListWidgetItem(nick)
            self._user_list.addItem(item)

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
