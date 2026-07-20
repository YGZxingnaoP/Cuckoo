# -*- coding: utf-8 -*-
"""
启动对话框
用户选择"房主模式"或"房客模式"，输入昵称；房客需输入房主 IP。
"""

import os
import winreg

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QRadioButton,
    QButtonGroup, QLabel, QLineEdit, QPushButton,
    QGroupBox, QMessageBox
)
from PySide6.QtCore import Qt


class StartDialog(QDialog):
    """
    模态启动对话框。
    确认后将 (is_host, peer_ip, nickname) 通过 result 属性返回。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cuckoo — 选择角色")
        self.setFixedSize(420, 340)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        self._is_host = True
        self._peer_ip = ""
        self._nickname = ""

        self._init_ui()

    @property
    def is_host(self) -> bool:
        return self._is_host

    @property
    def peer_ip(self) -> str:
        return self._peer_ip.strip()

    @property
    def nickname(self) -> str:
        return self._nickname.strip() or ("房主" if self._is_host else "房客")

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── 角色选择 ──
        role_group = QGroupBox("选择角色")
        role_layout = QVBoxLayout(role_group)

        self._radio_host = QRadioButton("房主模式（Host）— 创建房间")
        self._radio_guest = QRadioButton("房客模式（Guest）— 加入房间")
        self._radio_host.setChecked(True)
        self._radio_host.setObjectName("radioHost")
        self._radio_guest.setObjectName("radioGuest")

        btn_group = QButtonGroup(self)
        btn_group.addButton(self._radio_host)
        btn_group.addButton(self._radio_guest)

        role_layout.addWidget(self._radio_host)
        role_layout.addWidget(self._radio_guest)
        layout.addWidget(role_group)

        # ── 昵称输入 ──
        nick_layout = QHBoxLayout()
        self._nick_label = QLabel("昵称：")
        self._nick_edit = QLineEdit()
        self._nick_edit.setPlaceholderText("输入你的昵称（可选）")
        self._nick_edit.setMaxLength(20)
        self._nick_edit.setObjectName("nickEdit")
        nick_layout.addWidget(self._nick_label)
        nick_layout.addWidget(self._nick_edit)
        layout.addLayout(nick_layout)

        # ── IP 输入 ──
        ip_layout = QHBoxLayout()
        self._ip_label = QLabel("房主 IP：")
        self._ip_edit = QLineEdit()
        self._ip_edit.setPlaceholderText("例如 10.0.0.2（房客模式必填）")
        self._ip_edit.setObjectName("ipEdit")
        self._ip_edit.setEnabled(False)
        ip_layout.addWidget(self._ip_label)
        ip_layout.addWidget(self._ip_edit)
        layout.addLayout(ip_layout)

        # ── 启动 Radmin VPN ──
        radmin_layout = QHBoxLayout()
        self._btn_radmin = QPushButton("启动 Radmin VPN")
        self._btn_radmin.setObjectName("btnRadmin")
        self._btn_radmin.clicked.connect(self._on_launch_radmin)
        self._radmin_hint = QLabel("")
        self._radmin_hint.setStyleSheet("color: #999; font-size: 11px;")
        radmin_layout.addWidget(self._btn_radmin)
        radmin_layout.addWidget(self._radmin_hint, stretch=1)
        layout.addLayout(radmin_layout)

        # ── 确认按钮 ──
        self._btn_confirm = QPushButton("确认连接")
        self._btn_confirm.setObjectName("btnConfirm")
        self._btn_confirm.clicked.connect(self._on_confirm)
        layout.addWidget(self._btn_confirm)

        # ── 信号 ──
        self._radio_host.toggled.connect(self._on_role_changed)

    # ─────────────────────────────────────────
    # Radmin VPN 启动
    # ─────────────────────────────────────────

    def _on_launch_radmin(self) -> None:
        """尝试启动 Radmin VPN，找不到则非阻塞提示。"""
        exe = self._find_radmin()
        if exe:
            try:
                os.startfile(exe)
                self._radmin_hint.setText("已启动")
                self._radmin_hint.setStyleSheet("color: #4a4; font-size: 11px;")
            except OSError as e:
                self._radmin_hint.setText(f"启动失败: {e}")
                self._radmin_hint.setStyleSheet("color: #c44; font-size: 11px;")
        else:
            self._radmin_hint.setText("未找到 Radmin VPN，请手动启动")
            self._radmin_hint.setStyleSheet("color: #c44; font-size: 11px;")

    @staticmethod
    def _find_radmin() -> str:
        """通过注册表和开始菜单快捷方式查找 Radmin VPN。"""
        # 1) 注册表 App Paths（系统级程序注册）
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\RadminVPN.exe"
            )
            path = winreg.QueryValue(key, None)
            winreg.CloseKey(key)
            if path and os.path.isfile(path):
                return path
        except (OSError, FileNotFoundError):
            pass

        # 2) 注册表 Uninstall（查找 DisplayIcon 或 InstallLocation）
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for wow in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    parent = winreg.OpenKey(hive, wow)
                    for i in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            sub = winreg.OpenKey(parent, winreg.EnumKey(parent, i))
                            try:
                                name, _ = winreg.QueryValueEx(sub, "DisplayName")
                            except OSError:
                                winreg.CloseKey(sub)
                                continue
                            if "Radmin VPN" in str(name):
                                # 优先取 DisplayIcon（通常是 exe 路径）
                                for vname in ("DisplayIcon", "InstallLocation"):
                                    try:
                                        val, _ = winreg.QueryValueEx(sub, vname)
                                        val = str(val).strip().strip('"')
                                        if vname == "InstallLocation":
                                            val = os.path.join(val, "RadminVPN.exe")
                                        if os.path.isfile(val):
                                            winreg.CloseKey(sub)
                                            winreg.CloseKey(parent)
                                            return val
                                    except OSError:
                                        continue
                            winreg.CloseKey(sub)
                        except OSError:
                            continue
                    winreg.CloseKey(parent)
                except OSError:
                    pass

        # 3) 开始菜单快捷方式（精确匹配 "radmin vpn"，避免误匹配 Radmin Viewer）
        for base in (os.environ.get("ProgramData", ""),
                     os.environ.get("APPDATA", "")):
            lnk_dir = os.path.join(base, r"Microsoft\Windows\Start Menu\Programs")
            if not os.path.isdir(lnk_dir):
                continue
            for root, dirs, files in os.walk(lnk_dir):
                for f in files:
                    if "radmin vpn" in f.lower() and f.endswith(".lnk"):
                        return os.path.join(root, f)

        return ""

    # ─────────────────────────────────────────
    # 角色与确认
    # ─────────────────────────────────────────

    def _on_role_changed(self, is_host: bool) -> None:
        self._ip_edit.setEnabled(not is_host)
        if is_host:
            self._ip_edit.clear()

    def _on_confirm(self) -> None:
        self._nickname = self._nick_edit.text().strip()

        if self._radio_host.isChecked():
            self._is_host = True
            self._peer_ip = ""
            if not self._nickname:
                self._nickname = "房主"
            self.accept()
        else:
            ip = self._ip_edit.text().strip()
            if not ip:
                QMessageBox.warning(self, "错误", "请输入房主 Radmin VPN 的 IP 地址。")
                return
            self._is_host = False
            self._peer_ip = ip
            if not self._nickname:
                self._nickname = "房客"
            self.accept()
