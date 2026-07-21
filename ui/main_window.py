# -*- coding: utf-8 -*-
"""
主窗口（控制层）
星型拓扑架构：
  - 房主：运行 Server + ScreenHost + AudioMixer + HostFileHandler
  - 房客：运行 ClientConnection + ScreenGuest + GuestAudio
协调所有功能模块的启停，接收信号更新界面。
"""

import os
import sys
import struct
import threading
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QPushButton,
    QMessageBox, QStatusBar, QHBoxLayout, QWidget
)
from PySide6.QtCore import Qt, Signal

import socket
import config
from common import logger as log
from common import network

# 核心模块
from core.server import Server
from core.client import ClientConnection
from core.protocol import (
    NicknameRegistry,
    MSG_TEXT, MSG_FILE_META, MSG_FILE_CHUNK, MSG_SCREEN_FRAME, MSG_COMMAND, MSG_VOICE,
    CMD_SCREEN_START, CMD_SCREEN_STOP, HOST_ID, BROADCAST_ID,
    build_frame
)

# UI Tabs
from ui.tabs.screen_tab import ScreenTab
from ui.tabs.voice_tab import VoiceTab
from ui.tabs.file_tab import FileTab
from ui.tabs.chat_tab import ChatTab
from ui.online_panel import OnlineListPanel

# 功能模块
from func.screen_share.host import ScreenHost
from func.screen_share.guest import ScreenGuest
from func.voice_chat.mixer import AudioMixer
from func.voice_chat.guest_audio import GuestAudio
from func.file_transfer.host_file import HostFileHandler
from func.file_transfer.guest_file import GuestFileReceiver

TAG = "MainWindow"


class MainWindow(QMainWindow):
    """
    应用主窗口。
    负责：
    1. 创建 QTabWidget 布局
    2. 根据角色创建 Server（房主）或 ClientConnection（房客）
    3. 注册消息处理器，协调功能模块
    4. 管理生命周期
    """

    # 线程安全 UI 更新信号（供后台线程 emit → 主线程 slot）
    _send_status = Signal(str)
    _send_progress = Signal(int, str, str)
    _client_event = Signal(str)  # 房客加入/离开事件文本
    _targets_changed = Signal(list)  # 房客加入/离开时更新目标列表
    _online_users_changed = Signal(dict)  # 在线用户列表变更 {uid: nickname}
    _voice_data_received = Signal(bytes)  # 语音独立 TCP 接收到的混合音频

    # 房主断开连接信号（房客端专用）
    host_disconnected = Signal()

    def __init__(self, is_host: bool, peer_ip: str = "", nickname: str = ""):
        super().__init__()
        self._is_host = is_host
        self._peer_ip = peer_ip
        self._nickname = nickname
        self._role_name = "房主" if is_host else "房客"

        # 昵称注册表
        self._nicknames = NicknameRegistry()
        self._nicknames.set(HOST_ID, nickname if is_host else "房主")
        self._my_id = HOST_ID if is_host else -1

        # 核心模块实例
        self._server: Optional[Server] = None
        self._client: Optional[ClientConnection] = None

        # 功能模块实例
        self._screen_host: Optional[ScreenHost] = None
        self._screen_guest: Optional[ScreenGuest] = None
        self._audio_mixer: Optional[AudioMixer] = None
        self._guest_audio: Optional[GuestAudio] = None
        self._host_file: Optional[HostFileHandler] = None
        self._guest_file_recv: Optional[GuestFileReceiver] = None

        # 主机麦克风采集线程
        self._host_mic_running = False
        self._host_mic_thread: Optional[threading.Thread] = None

        # 语音独立 TCP 连接（房客端）
        self._voice_sock: Optional[socket.socket] = None
        self._voice_send_thread: Optional[threading.Thread] = None
        self._voice_recv_thread: Optional[threading.Thread] = None

        self._init_ui()
        self._start_services()
        self._update_online_panel()

    # ═════════════════════════════════════════
    # UI 构建
    # ═════════════════════════════════════════

    def _init_ui(self) -> None:
        self.setWindowTitle(f"Cuckoo — {self._role_name}模式")
        self.resize(config.WINDOW_WIDTH, config.WINDOW_HEIGHT)

        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("mainTabs")

        # Tab 1: 投屏
        self._screen_tab = ScreenTab(is_host=self._is_host)
        self._tabs.addTab(self._screen_tab, "投屏")

        # Tab 2: 语音
        self._voice_tab = VoiceTab()
        self._tabs.addTab(self._voice_tab, "语音")

        # Tab 3: 文件
        self._file_tab = FileTab()
        self._tabs.addTab(self._file_tab, "文件")

        # Tab 4: 文字
        self._chat_tab = ChatTab()
        self._tabs.addTab(self._chat_tab, "文字")

        # 在线列表面板（侧边栏）
        self._online_panel = OnlineListPanel(is_host=self._is_host)

        main_layout.addWidget(self._online_panel)
        main_layout.addWidget(self._tabs, stretch=1)
        self.setCentralWidget(central)

        # ── 状态栏 ──
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage(f"角色：{self._role_name} ({self._nickname})")

        # 在线状态检测按钮（仅房客）
        if not self._is_host:
            self._btn_probe = QPushButton("检测在线状态")
            self._btn_probe.setObjectName("btnProbe")
            self._btn_probe.clicked.connect(self._on_probe)
            self._status_bar.addPermanentWidget(self._btn_probe)

        # ── 信号绑定 ──
        self._screen_tab.toggle_requested.connect(self._on_toggle_screen)
        self._screen_tab.resolution_changed.connect(self._on_resolution_changed)
        self._screen_tab.fps_changed.connect(self._on_fps_changed)
        self._voice_tab.toggle_mic_requested.connect(self._on_toggle_mic)
        self._chat_tab.send_requested.connect(self._on_chat_send)
        self._file_tab.file_send_requested.connect(self._on_file_send)
        self._file_tab.folder_send_requested.connect(self._on_folder_send)

        # 线程安全 UI 更新信号连接
        self._send_status.connect(self._file_tab.set_status)
        self._send_progress.connect(self._file_tab.update_progress)
        self._client_event.connect(self._on_client_event_ui)
        self._targets_changed.connect(self._on_targets_changed_ui)
        self._online_users_changed.connect(self._online_panel.update_users)

        # 音量增益信号
        self._voice_tab.volume_gain_changed.connect(self._on_volume_gain_changed)

        # 语音数据接收信号（从独立 TCP 线程发出）
        self._voice_data_received.connect(self._on_voice_data_received)

    # ═════════════════════════════════════════
    # 服务启动
    # ═════════════════════════════════════════

    def _start_services(self) -> None:
        """根据角色启动服务。"""
        if self._is_host:
            self._start_host_services()
        else:
            self._start_guest_services()

    # ─────────────────────────────────────────
    # 房主服务
    # ─────────────────────────────────────────

    def _start_host_services(self) -> None:
        """房主：启动 TCP 服务器 + UDP 混音器。"""
        try:
            # 创建 Server
            self._server = Server(host_nickname=self._nickname)
            self._server.set_on_client_joined(self._on_client_joined)
            self._server.set_on_client_left(self._on_client_left)

            # 注册文本消息处理器
            self._server.register_handler(MSG_TEXT, self._handle_text)

            # 注册语音消息处理器
            self._server.register_handler(MSG_VOICE, self._handle_voice)

            # 创建投屏模块
            self._screen_host = ScreenHost(self._server)

            # 创建混音器（使用 TCP 发送回调）
            self._audio_mixer = AudioMixer(send_callback=self._send_voice_to_client)
            self._audio_mixer.start()
            self._audio_mixer.start_playback()  # 自动开始回放，主机始终能听到房客声音

            # 创建文件处理器
            self._host_file = HostFileHandler(self._server)
            self._host_file.progress.connect(self._file_tab.update_progress)
            self._host_file.file_complete.connect(self._on_file_complete)
            self._host_file.status_changed.connect(self._file_tab.set_status)

            # 启动服务器
            self._server.start()

            # 初始化聊天
            self._my_id = HOST_ID
            self._chat_tab.setup(HOST_ID, self._nicknames)

            self._status_bar.showMessage("房间已创建 — 等待房客加入")
            log.log(TAG, "Host services started")

        except OSError as e:
            log.error(TAG, f"Port bind failed: {e}")
            QMessageBox.critical(self, "端口错误", f"端口绑定失败：{e}\n请检查防火墙设置或端口是否被占用。")
            sys.exit(1)

    def _on_client_joined(self, uid: int, nickname: str) -> None:
        """房客加入回调（从 Server 线程调用）→ 通过信号安全更新 UI。"""
        self._nicknames.set(uid, nickname)
        targets = {u: info.nickname or f"房客{u}" for u, info in self._server.clients.items()}
        self._targets_changed.emit(list(targets.items()))
        self._online_users_changed.emit(self._nicknames.get_all())
        self._client_event.emit(f"{nickname} 加入了房间")

    def _on_client_left(self, uid: int, nickname: str) -> None:
        """房客离开回调。"""
        self._nicknames.remove(uid)
        if self._audio_mixer:
            self._audio_mixer.unregister_client(uid)
        targets = {u: info.nickname or f"房客{u}" for u, info in self._server.clients.items()} if self._server else {}
        self._targets_changed.emit(list(targets.items()))
        self._online_users_changed.emit(self._nicknames.get_all())
        self._client_event.emit(f"{nickname} 离开了房间")

    def _on_client_event_ui(self, text: str) -> None:
        """主线程槽：处理房客加入/离开事件的 UI 更新。"""
        self._chat_tab.append_system(text)
        if self._server:
            self._status_bar.showMessage(f"{text} — 当前 {len(self._server.clients)} 人在线")

    def _on_targets_changed_ui(self, targets_list: list) -> None:
        """主线程槽：更新文件发送目标列表。"""
        self._file_tab.update_targets(dict(targets_list))

    # ─────────────────────────────────────────
    # 房客服务
    # ─────────────────────────────────────────

    def _start_guest_services(self) -> None:
        """房客：连接房主。"""
        try:
            # 创建 ClientConnection
            self._client = ClientConnection(self._peer_ip, self._nickname)

            # 连接信号
            self._client.joined.connect(self._on_joined)
            self._client.user_list.connect(self._on_user_list)
            self._client.user_joined.connect(self._on_user_joined)
            self._client.user_left.connect(self._on_user_left)
            self._client.frame_received.connect(self._on_frame_received)
            self._client.disconnected.connect(self._on_disconnected)

            # 创建投屏接收器
            self._screen_guest = ScreenGuest()
            self._screen_guest.frame_ready.connect(self._screen_tab.update_frame)
            self._screen_guest.start()

            # 连接到房主
            self._client.connect_to_host()

            self._status_bar.showMessage("正在连接房主...")
            log.log(TAG, "Guest services starting")

        except (ConnectionRefusedError, OSError) as e:
            log.error(TAG, f"Connection failed: {e}")
            QMessageBox.critical(self, "连接失败", f"无法连接房主：{e}")
            sys.exit(1)

    def _on_joined(self, assigned_id: int, nickname: str) -> None:
        """成功加入房间。"""
        self._my_id = assigned_id
        self._nicknames.set(assigned_id, nickname)
        self._chat_tab.setup(assigned_id, self._nicknames)
        self._chat_tab.append_system(f"已加入房间，你的ID是 {assigned_id}")

        # 建立独立语音 TCP 连接
        self._connect_voice_tcp(assigned_id)

        # 初始化音频（打开扬声器输出，但不开麦）
        self._guest_audio = GuestAudio(self._client, assigned_id, self._voice_sock)
        self._guest_audio.open_output()  # 立即可收听房主语音

        # 初始化文件接收器
        self._guest_file_recv = GuestFileReceiver()
        self._guest_file_recv.progress.connect(self._file_tab.update_progress)
        self._guest_file_recv.file_complete.connect(self._on_file_complete)
        self._guest_file_recv.status_changed.connect(self._file_tab.set_status)

        self._status_bar.showMessage(f"已连接房主 — ID: {assigned_id}")
        log.log(TAG, f"Joined as ID={assigned_id}")

    def _on_user_list(self, users_list: list) -> None:
        """收到在线用户列表。"""
        users = dict(users_list)
        for uid, nick in users.items():
            self._nicknames.set(uid, nick)
        self._online_users_changed.emit(self._nicknames.get_all())
        self._update_guest_file_targets(users)

    def _on_user_joined(self, uid: int, nickname: str) -> None:
        """新用户加入通知。"""
        self._nicknames.set(uid, nickname)
        self._chat_tab.append_system(f"{nickname} 加入了房间")
        self._online_users_changed.emit(self._nicknames.get_all())
        # 更新目标列表
        all_users = self._nicknames.get_all()
        all_users.pop(self._my_id, None)
        self._file_tab.update_targets(all_users)

    def _on_user_left(self, uid: int, nickname: str) -> None:
        """用户离开通知。"""
        self._nicknames.remove(uid)
        self._chat_tab.append_system(f"{nickname} 离开了房间")
        self._online_users_changed.emit(self._nicknames.get_all())
        all_users = self._nicknames.get_all()
        all_users.pop(self._my_id, None)
        self._file_tab.update_targets(all_users)

    def _update_guest_file_targets(self, users: dict) -> None:
        """房客端更新文件目标（排除自己）。"""
        targets = {uid: nick for uid, nick in users.items() if uid != self._my_id}
        self._file_tab.update_targets(targets)

    def _on_disconnected(self) -> None:
        """连接断开——房客跳回启动界面。"""
        self._status_bar.showMessage("与房主断开连接")
        self._screen_tab.stop_streaming()
        self._close_voice_tcp()
        QMessageBox.warning(self, "连接断开", "与房主的连接已断开，将返回启动界面。")
        self.host_disconnected.emit()
        self.close()

    # ═════════════════════════════════════════
    # 语音独立 TCP 连接（房客端）
    # ═════════════════════════════════════════

    def _connect_voice_tcp(self, assigned_id: int) -> None:
        """建立独立语音 TCP 连接并启动收发线程。"""
        try:
            self._voice_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._voice_sock.settimeout(5.0)
            self._voice_sock.connect((self._peer_ip, config.VOICE_TCP_PORT))
            self._voice_sock.settimeout(None)

            # 发送 uid 标识
            self._voice_sock.sendall(struct.pack("!I", assigned_id))

            # 读取 ACK
            ack = self._voice_sock.recv(1)
            if not ack:
                raise ConnectionError("Voice TCP: no ACK from server")

            log.log(TAG, f"Voice TCP connected on port {config.VOICE_TCP_PORT}")

            # 启动语音接收线程
            self._voice_recv_thread = threading.Thread(
                target=self._voice_recv_loop, daemon=True, name="GuestVoiceRecv"
            )
            self._voice_recv_thread.start()

        except Exception as e:
            log.error(TAG, f"Voice TCP connection failed: {e}")
            self._voice_sock = None

    def _voice_recv_loop(self) -> None:
        """语音接收线程：从独立 TCP 连接读取混合音频并通过信号分发给 UI。"""
        from core.protocol import read_frame as proto_read_frame
        log.log(TAG, "Voice recv loop started")

        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = self._voice_sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("Voice TCP closed")
                buf.extend(chunk)
            return bytes(buf)

        try:
            while self._voice_sock:
                result = proto_read_frame(recv_exact)
                if result is None:
                    break
                msg_type, sender_id, target_id, payload = result
                if msg_type == MSG_VOICE:
                    self._voice_data_received.emit(payload)
        except (ConnectionError, OSError) as e:
            log.error(TAG, f"Voice recv error: {e}")
        log.log(TAG, "Voice recv loop stopped")

    def _on_voice_data_received(self, pcm_data: bytes) -> None:
        """主线程槽：播放从语音 TCP 收到的混合音频。"""
        if self._guest_audio:
            self._guest_audio.play_mixed_audio(pcm_data)

    def _close_voice_tcp(self) -> None:
        """关闭语音 TCP 连接。"""
        if self._voice_sock:
            try:
                self._voice_sock.close()
            except OSError:
                pass
            self._voice_sock = None
        if self._voice_recv_thread:
            self._voice_recv_thread.join(timeout=2)
            self._voice_recv_thread = None
        if self._voice_send_thread:
            self._voice_send_thread.join(timeout=2)
            self._voice_send_thread = None

    # ═════════════════════════════════════════
    # 帧处理（房客端）
    # ═════════════════════════════════════════

    def _on_frame_received(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        """房客收到帧（由 ClientConnection 主 TCP 分发，语音已通过独立 TCP 处理）。"""
        if msg_type == MSG_TEXT:
            self._handle_text_guest(sender_id, payload)
        elif msg_type == MSG_SCREEN_FRAME:
            if self._screen_guest:
                self._screen_guest.push_frame_data(payload)
        elif msg_type == MSG_FILE_META:
            if self._guest_file_recv:
                self._guest_file_recv.handle_file_meta(sender_id, payload)
        elif msg_type == MSG_FILE_CHUNK:
            if self._guest_file_recv:
                self._guest_file_recv.handle_file_chunk(sender_id, payload)

    def _handle_text_guest(self, sender_id: int, payload: bytes) -> None:
        """房客处理文本消息。"""
        try:
            text = payload.decode("utf-8")
            self._chat_tab.append_message(sender_id, text)
        except UnicodeDecodeError:
            log.warn(TAG, "Discarded non-UTF-8 text message")

    # ═════════════════════════════════════════
    # 文本消息处理（房主端）
    # ═════════════════════════════════════════

    def _handle_text(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        """房主收到文本消息 → 广播给所有人。"""
        # 广播给其他房客
        broadcast_frame = build_frame(MSG_TEXT, sender_id, BROADCAST_ID, payload)
        self._server.broadcast(broadcast_frame, exclude={sender_id}, msg_type=MSG_TEXT)
        # 房主自己也显示
        try:
            text = payload.decode("utf-8")
            self._chat_tab.append_message(sender_id, text)
        except UnicodeDecodeError:
            pass

    def _handle_voice(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        """房主收到语音数据 → 推送给混音器。"""
        if self._audio_mixer:
            self._audio_mixer.push_client_audio(sender_id, payload)

    def _send_voice_to_client(self, uid: int, pcm_data: bytes) -> None:
        """混音器回调：通过独立语音 TCP 发送混合音频给指定房客。"""
        if self._server:
            with self._server._clients_lock:
                info = self._server._clients.get(uid)
            if info and info.voice_sock:
                frame = build_frame(MSG_VOICE, HOST_ID, uid, pcm_data)
                try:
                    if info.voice_send_queue.full():
                        try:
                            info.voice_send_queue.get_nowait()
                        except Exception:
                            pass
                    info.voice_send_queue.put_nowait(frame)
                except Exception:
                    pass

    # ═════════════════════════════════════════
    # 事件处理
    # ═════════════════════════════════════════

    def _on_toggle_screen(self) -> None:
        """房主投屏开关。"""
        if not self._is_host:
            return
        if self._screen_host and self._screen_host.running:
            self._screen_host.stop()
            self._screen_tab.stop_streaming()
            # 发送停止投屏信令
            if self._server:
                stop_frame = build_frame(MSG_COMMAND, HOST_ID, BROADCAST_ID,
                                         bytes([CMD_SCREEN_STOP]))
                self._server.broadcast(stop_frame, msg_type=MSG_COMMAND)
            log.log(TAG, "Screen sharing stopped")
        else:
            if self._screen_host:
                self._screen_host.start()
                self._screen_tab.start_streaming()
                # 发送开始投屏信令
                if self._server:
                    start_frame = build_frame(MSG_COMMAND, HOST_ID, BROADCAST_ID,
                                              bytes([CMD_SCREEN_START]))
                    self._server.broadcast(start_frame, msg_type=MSG_COMMAND)
                log.log(TAG, "Screen sharing started")
            else:
                QMessageBox.warning(self, "提示", "服务器尚未就绪。")

    def _on_resolution_changed(self, width: int, height: int) -> None:
        """投屏分辨率变更。"""
        if self._screen_host:
            self._screen_host.set_resolution(width, height)

    def _on_fps_changed(self, fps: int) -> None:
        """投屏帧率变更。"""
        if self._screen_host:
            self._screen_host.set_fps(fps)

    def _on_toggle_mic(self) -> None:
        """麦克风开关。"""
        if self._is_host:
            self._toggle_host_mic()
        else:
            self._toggle_guest_mic()

    def _toggle_host_mic(self) -> None:
        """房主麦克风切换（仅控制采集，回放始终开启）。"""
        if self._host_mic_running:
            self._host_mic_running = False
            self._voice_tab.set_mic_off()
            # 等待采集线程彻底退出并释放 PyAudio 资源
            if self._host_mic_thread and self._host_mic_thread.is_alive():
                self._host_mic_thread.join(timeout=3)
            self._host_mic_thread = None
            log.log(TAG, "Host mic off")
        else:
            if not self._audio_mixer:
                QMessageBox.warning(self, "提示", "混音器未就绪。")
                return
            # 确保上一个采集线程已退出
            if self._host_mic_thread and self._host_mic_thread.is_alive():
                self._host_mic_thread.join(timeout=3)
            self._host_mic_running = True
            self._voice_tab.set_mic_on()
            self._host_mic_thread = threading.Thread(
                target=self._host_mic_loop, daemon=True, name="HostMic"
            )
            self._host_mic_thread.start()
            log.log(TAG, "Host mic on")

    def _host_mic_loop(self) -> None:
        """房主麦克风采集循环。"""
        import pyaudio
        try:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=config.AUDIO_CHANNELS,
                rate=config.AUDIO_RATE,
                input=True,
                frames_per_buffer=config.AUDIO_CHUNK
            )
            log.log(TAG, "Host mic loop started")
            count = 0
            while self._host_mic_running:
                pcm = stream.read(config.AUDIO_CHUNK, exception_on_overflow=False)
                if self._audio_mixer:
                    self._audio_mixer.push_host_audio(pcm)
                count += 1
                if count % 50 == 1:
                    log.log(TAG, f"[HOST-MIC] chunk#{count} len={len(pcm)}")
            stream.stop_stream()
            stream.close()
            pa.terminate()
            log.log(TAG, f"Host mic loop stopped (total={count})")
        except Exception as e:
            log.error(TAG, f"Host mic error: {e}")
            self._host_mic_running = False

    def _toggle_guest_mic(self) -> None:
        """房客麦克风切换。"""
        if not self._guest_audio:
            QMessageBox.warning(self, "提示", "尚未加入房间。")
            return
        if self._guest_audio.mic_on:
            self._guest_audio.stop_mic()
            self._voice_tab.set_mic_off()
            log.log(TAG, "Guest mic off")
        else:
            if self._guest_audio.start_mic():
                self._voice_tab.set_mic_on()
                log.log(TAG, "Guest mic on")
            else:
                QMessageBox.warning(self, "音频错误", "无法开启音频设备。")

    def _on_chat_send(self, text: str) -> None:
        """发送文本消息。"""
        data = text.encode("utf-8")
        if self._is_host and self._server:
            # 房主：直接广播
            frame = build_frame(MSG_TEXT, HOST_ID, BROADCAST_ID, data)
            self._server.broadcast(frame, msg_type=MSG_TEXT)
        elif self._client:
            # 房客：发送给房主，由房主广播
            self._client.send_frame(MSG_TEXT, HOST_ID, data)

    def _on_file_send(self, file_path: str, target_id: int) -> None:
        """发送文件。"""
        if self._is_host and self._host_file:
            self._host_file.send_file(file_path, target_id)
        elif self._client:
            # 房客发送文件（通过 ClientConnection 发给房主中转）
            self._guest_send_file(file_path, target_id)

    def _on_folder_send(self, folder_path: str, target_id: int) -> None:
        """发送文件夹。"""
        if self._is_host and self._host_file:
            self._host_file.send_folder(folder_path, target_id)
        elif self._client:
            self._guest_send_folder(folder_path, target_id)

    def _guest_send_file(self, file_path: str, target_id: int) -> None:
        """房客发送文件给指定用户（通过房主中转）。"""
        import threading
        threading.Thread(
            target=self._guest_file_loop,
            args=(file_path, target_id, ""),
            daemon=True, name="GuestFileSend"
        ).start()

    def _guest_send_folder(self, folder_path: str, target_id: int) -> None:
        """房客发送文件夹给指定用户（通过房主中转）。"""
        import threading
        threading.Thread(
            target=self._guest_folder_loop,
            args=(folder_path, target_id),
            daemon=True, name="GuestFolderSend"
        ).start()

    def _guest_file_loop(self, file_path: str, target_id: int, base_name: str = "") -> None:
        """房客文件发送循环。"""
        try:
            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            name_bytes = filename.encode("utf-8")
            base_bytes = base_name.encode("utf-8")

            # 元数据: [4B base_name_len][base_name][4B name_len][name][8B size][4B target_id]
            meta = bytearray()
            meta.extend(struct.pack("!I", len(base_bytes)))
            meta.extend(base_bytes)
            meta.extend(struct.pack("!I", len(name_bytes)))
            meta.extend(name_bytes)
            meta.extend(struct.pack("!Q", file_size))
            meta.extend(struct.pack("!I", target_id))

            self._client.send_frame(MSG_FILE_META, BROADCAST_ID, bytes(meta))

            display = f"{base_name}/{filename}" if base_name else filename
            self._send_status.emit(f"正在发送: {display}")

            # 数据块（自适应限速：监控发送队列深度）
            import time
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(config.FILE_CHUNK_SIZE)
                    if not chunk:
                        break
                    self._client.send_frame(MSG_FILE_CHUNK, BROADCAST_ID, chunk)
                    # 自适应限速：队列积压时等待，空闲时快速发送
                    while self._client._send_queue.qsize() > config.SEND_QUEUE_MAX * 2:
                        time.sleep(0.005)

            self._send_status.emit(f"发送完成: {display}")
            self._send_progress.emit(100, "完成", "")
            log.log(TAG, f"File sent: {display} to {target_id}")

        except OSError as e:
            log.error(TAG, f"File send error: {e}")
            self._send_status.emit(f"发送失败: {e}")

    def _guest_folder_loop(self, folder_path: str, target_id: int) -> None:
        """房客文件夹发送循环（遍历文件夹逐个发送）。"""
        folder_name = os.path.basename(folder_path)
        file_list = []
        for root, dirs, files in os.walk(folder_path):
            for fname in files:
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, folder_path)
                file_list.append((full_path, rel_path))

        total = len(file_list)
        self._send_status.emit(f"正在发送文件夹: {folder_name} ({total} 个文件)")
        log.log(TAG, f"Sending folder '{folder_name}' with {total} files to {target_id}")

        import time
        for idx, (full_path, rel_path) in enumerate(file_list, 1):
            try:
                file_size = os.path.getsize(full_path)
                rel_bytes = rel_path.encode("utf-8")
                base_bytes = folder_name.encode("utf-8")

                # 元数据
                meta = bytearray()
                meta.extend(struct.pack("!I", len(base_bytes)))
                meta.extend(base_bytes)
                meta.extend(struct.pack("!I", len(rel_bytes)))
                meta.extend(rel_bytes)
                meta.extend(struct.pack("!Q", file_size))
                meta.extend(struct.pack("!I", target_id))

                self._client.send_frame(MSG_FILE_META, BROADCAST_ID, bytes(meta))
                self._send_status.emit(f"[{idx}/{total}] {rel_path}")

                # 数据块（自适应限速）
                with open(full_path, "rb") as f:
                    while True:
                        chunk = f.read(config.FILE_CHUNK_SIZE)
                        if not chunk:
                            break
                        self._client.send_frame(MSG_FILE_CHUNK, BROADCAST_ID, chunk)
                        while self._client._send_queue.qsize() > config.SEND_QUEUE_MAX * 2:
                            time.sleep(0.005)

                time.sleep(0.01)

            except OSError as e:
                log.error(TAG, f"File send error ({rel_path}): {e}")
                continue

        self._send_status.emit(f"文件夹发送完成: {folder_name} ({total} 个文件)")
        self._send_progress.emit(100, "完成", "")
        log.log(TAG, f"Folder sent: {folder_name} to {target_id}")

    def _on_file_complete(self, path: str) -> None:
        self._file_tab.set_status(f"接收完成: {os.path.basename(path)}")

    def _on_probe(self) -> None:
        """房客探测房主在线状态。"""
        if network.probe_tcp(self._peer_ip, config.TCP_PORT, timeout=2.0):
            self._status_bar.showMessage("房主在线")
        else:
            self._status_bar.showMessage("房主离线")

    def _on_volume_gain_changed(self, gain_percent: int) -> None:
        """音量增益变更处理。"""
        if self._guest_audio:
            self._guest_audio.set_volume_gain(gain_percent)
        if self._audio_mixer:
            self._audio_mixer.set_volume_gain(gain_percent)

    def _update_online_panel(self) -> None:
        """刷新在线列表面板。"""
        self._online_panel.update_users(self._nicknames.get_all())

    # ═════════════════════════════════════════
    # 窗口关闭
    # ═════════════════════════════════════════

    def closeEvent(self, event) -> None:
        """清理所有资源。"""
        log.log(TAG, "Shutting down...")

        # 停止投屏
        if self._screen_host:
            self._screen_host.stop()
        if self._screen_guest:
            self._screen_guest.stop()

        # 停止音频
        if self._audio_mixer:
            self._audio_mixer.stop()
        if self._guest_audio:
            self._guest_audio.stop()
        self._host_mic_running = False
        if self._host_mic_thread:
            self._host_mic_thread.join(timeout=3)

        # 关闭语音 TCP 连接
        self._close_voice_tcp()

        # 停止文件传输
        if self._host_file:
            self._host_file.cleanup()
        if self._guest_file_recv:
            self._guest_file_recv.cleanup()

        # 停止网络
        if self._server:
            self._server.stop()
        if self._client:
            self._client.stop()

        log.log(TAG, "Shutdown complete")
        event.accept()
