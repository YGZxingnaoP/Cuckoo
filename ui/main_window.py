# -*- coding: utf-8 -*-
import os
import sys
import struct
import threading
from typing import Optional

from PySide6.QtWidgets import QMainWindow, QTabWidget, QPushButton, QMessageBox, QStatusBar, QHBoxLayout, QWidget
from PySide6.QtCore import Qt, Signal, QTimer
import socket
import config
from common import logger as log
from common import network

from core.server import Server
from core.client import ClientConnection
from core.protocol import (
    NicknameRegistry, MSG_TEXT, MSG_SCREEN_FRAME, MSG_COMMAND, MSG_VOICE,
    CMD_SCREEN_START, CMD_SCREEN_STOP, HOST_ID, BROADCAST_ID, build_frame,
    MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK, MSG_FILE_CHUNK
)

from ui.tabs.screen_tab import ScreenTab
from ui.tabs.voice_tab import VoiceTab
from ui.tabs.file_tab import FileTab
from ui.tabs.chat_tab import ChatTab
from ui.online_panel import OnlineListPanel

from func.screen_share.host import ScreenHost
from func.screen_share.guest import ScreenGuest
from func.voice_chat.mixer import AudioMixer
from func.voice_chat.guest_audio import GuestAudio
from func.voice_chat.system_audio import SystemAudioCapture
from func.file_transfer.unified_file import UnifiedFileTransfer

TAG = "MainWindow"

class MainWindow(QMainWindow):
    _send_status = Signal(str)
    _send_progress = Signal(int, str, str)
    _client_event = Signal(str)
    _targets_changed = Signal(list)
    _online_users_changed = Signal(object)  # 【修复】使用 object 防止 PySide6 dict 转换崩溃
    _voice_data_received = Signal(bytes)
    _mic_error_signal = Signal(str)

    host_disconnected = Signal()

    def __init__(self, is_host: bool, peer_ip: str = "", nickname: str = ""):
        super().__init__()
        self._is_host = is_host
        self._peer_ip = peer_ip
        self._nickname = nickname
        self._role_name = "房主" if is_host else "房客"

        self._nicknames = NicknameRegistry()
        self._nicknames.set(HOST_ID, nickname if is_host else "房主")
        self._my_id = HOST_ID if is_host else -1

        self._server: Optional[Server] = None
        self._client: Optional[ClientConnection] = None
        self._screen_host: Optional[ScreenHost] = None
        self._screen_guest: Optional[ScreenGuest] = None
        self._audio_mixer: Optional[AudioMixer] = None
        self._guest_audio: Optional[GuestAudio] = None
        self._file_manager: Optional[UnifiedFileTransfer] = None  # 【重构】统一文件管理器

        self._host_mic_running = False
        self._host_mic_thread: Optional[threading.Thread] = None
        self._host_input_device_index: int = -1
        self._host_output_device_index: int = -1

        self._voice_sock: Optional[socket.socket] = None
        self._voice_recv_thread: Optional[threading.Thread] = None
        self._system_audio_capture: Optional[SystemAudioCapture] = None

        self._init_ui()
        self._start_services()
        self._populate_audio_devices()
        self._update_online_panel()

    def _init_ui(self) -> None:
        self.setWindowTitle(f"Cuckoo — {self._role_name}模式")
        self.resize(config.WINDOW_WIDTH, config.WINDOW_HEIGHT)

        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("mainTabs")

        self._screen_tab = ScreenTab(is_host=self._is_host)
        self._tabs.addTab(self._screen_tab, "投屏")
        self._voice_tab = VoiceTab()
        self._tabs.addTab(self._voice_tab, "语音")
        self._file_tab = FileTab()
        self._tabs.addTab(self._file_tab, "文件")
        self._chat_tab = ChatTab()
        self._tabs.addTab(self._chat_tab, "文字")

        self._online_panel = OnlineListPanel(is_host=self._is_host)
        main_layout.addWidget(self._online_panel)
        main_layout.addWidget(self._tabs, stretch=1)
        self.setCentralWidget(central)

        self._btn_expand_panel = QPushButton("三")
        self._btn_expand_panel.setParent(self._tabs)
        self._btn_expand_panel.setFixedSize(24, 24)
        self._btn_expand_panel.move(2, 2)
        self._btn_expand_panel.raise_()
        self._btn_expand_panel.setStyleSheet("QPushButton { font-size: 12px; padding: 0; border: none; background: transparent; color: #888; } QPushButton:hover { color: #f0f0f0; background: #1a1a1a; border-radius: 3px; }")
        self._btn_expand_panel.setToolTip("展开在线列表")
        self._btn_expand_panel.clicked.connect(self._online_panel.toggle_collapse)
        self._btn_expand_panel.hide()
        self._online_panel.collapsed_changed.connect(self._on_panel_collapsed_changed)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage(f"角色：{self._role_name} ({self._nickname})")

        if not self._is_host:
            self._btn_probe = QPushButton("检测在线状态")
            self._btn_probe.setObjectName("btnProbe")
            self._btn_probe.clicked.connect(self._on_probe)
            self._status_bar.addPermanentWidget(self._btn_probe)

        self._screen_tab.toggle_requested.connect(self._on_toggle_screen)
        self._screen_tab.resolution_changed.connect(self._on_resolution_changed)
        self._screen_tab.fps_changed.connect(self._on_fps_changed)
        self._voice_tab.toggle_mic_requested.connect(self._on_toggle_mic)
        self._chat_tab.send_requested.connect(self._on_chat_send)
        
        # 文件信号连接
        self._file_tab.file_send_requested.connect(self._on_file_send)
        self._file_tab.folder_send_requested.connect(self._on_folder_send)
        self._file_tab.resume_requested.connect(self._on_resume_requested)
        self._file_tab.clear_requested.connect(self._on_clear_requested)

        self._client_event.connect(self._on_client_event_ui)
        self._targets_changed.connect(self._on_targets_changed_ui)
        self._online_users_changed.connect(self._online_panel.update_users)
        self._voice_tab.volume_gain_changed.connect(self._on_volume_gain_changed)
        self._voice_tab.device_changed.connect(self._on_voice_device_changed)
        
        if self._is_host:
            self._screen_tab.share_audio_toggled.connect(self._on_share_audio_toggled)
            self._screen_tab.speaker_changed.connect(self._on_speaker_changed)

        self._voice_data_received.connect(self._on_voice_data_received)
        self._mic_error_signal.connect(self._on_mic_error)

        self._online_refresh_timer = QTimer(self)
        self._online_refresh_timer.setInterval(3000)
        self._online_refresh_timer.timeout.connect(self._on_online_refresh_tick)
        self._online_refresh_timer.start()

    def _start_services(self) -> None:
        if self._is_host: self._start_host_services()
        else: self._start_guest_services()

    def _init_file_manager(self, send_callback):
        """【重构】统一初始化文件管理器"""
        self._file_manager = UnifiedFileTransfer(self._my_id, send_callback)
        self._file_manager.progress.connect(self._file_tab.update_progress)
        self._file_manager.file_complete.connect(self._on_file_complete)
        self._file_manager.status_changed.connect(self._on_file_status_changed)
        self._file_manager.task_interrupted.connect(self._file_tab.add_interrupted_task)
        self._file_manager.task_removed.connect(self._file_tab.remove_interrupted_task)

    def _on_file_status_changed(self, status: str):
        self._file_tab.set_status(status)
        # 【体验优化】：房客接收文件时，暂停解码渲染防止卡顿
        if status.startswith("正在接收"):
            if not self._is_host and self._screen_tab._streaming:
                self._screen_tab.stop_streaming()
                if self._screen_guest: self._screen_guest.stop()
                self._chat_tab.append_system("正在接收文件，已自动暂停投屏渲染")

    # ═════════════════════════════════════════
    # 房主服务
    # ═════════════════════════════════════════
    def _start_host_services(self) -> None:
        try:
            self._server = Server(host_nickname=self._nickname)
            self._server.set_on_client_joined(self._on_client_joined)
            self._server.set_on_client_left(self._on_client_left)

            self._server.register_handler(MSG_TEXT, self._handle_text)
            self._server.register_handler(MSG_VOICE, self._handle_voice)
            
            # 注册文件处理器 (透明中转 + 本地处理)
            for msg_t in (MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK, MSG_FILE_CHUNK):
                self._server.register_handler(msg_t, self._handle_file)

            self._screen_host = ScreenHost(self._server)
            self._audio_mixer = AudioMixer(send_callback=self._send_voice_to_client, output_device_index=self._host_output_device_index)
            self._audio_mixer.set_get_client_ids_callback(self._get_online_client_ids)
            self._audio_mixer.start()
            self._audio_mixer.start_playback()

            # 房主文件发送回调
            def _host_file_cb(msg_type, target_id, payload):
                if target_id == HOST_ID:
                    self._file_manager.handle_incoming(msg_type, HOST_ID, payload)
                else:
                    frame = build_frame(msg_type, HOST_ID, target_id, payload)
                    self._server.send_to(target_id, frame, msg_type)
            
            self._init_file_manager(_host_file_cb)

            self._server.start()
            self._my_id = HOST_ID
            self._chat_tab.setup(HOST_ID, self._nicknames)
            self._status_bar.showMessage("房间已创建 — 等待房客加入")
            log.log(TAG, "Host services started")
        except OSError as e:
            log.error(TAG, f"Port bind failed: {e}")
            QMessageBox.critical(self, "端口错误", f"端口绑定失败：{e}\n请检查防火墙设置或端口是否被占用。")
            sys.exit(1)

    def _handle_file(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        """房主文件处理：本地接收或透明中转"""
        if not self._file_manager: return
        if target_id == HOST_ID:
            self._file_manager.handle_incoming(msg_type, sender_id, payload)
        else:
            relay = build_frame(msg_type, sender_id, target_id, payload)
            self._server.send_to(target_id, relay, msg_type)

    def _on_client_joined(self, uid: int, nickname: str) -> None:
        self._nicknames.set(uid, nickname)
        targets = {u: info.nickname or f"房客{u}" for u, info in self._server.clients.items()}
        self._targets_changed.emit(list(targets.items()))
        self._online_users_changed.emit(dict(self._nicknames.get_all()))
        self._client_event.emit(f"{nickname} 加入了房间")

    def _on_client_left(self, uid: int, nickname: str) -> None:
        self._nicknames.remove(uid)
        if self._audio_mixer: self._audio_mixer.unregister_client(uid)
        targets = {u: info.nickname or f"房客{u}" for u, info in self._server.clients.items()} if self._server else {}
        self._targets_changed.emit(list(targets.items()))
        self._online_users_changed.emit(dict(self._nicknames.get_all()))
        self._client_event.emit(f"{nickname} 离开了房间")

    # ═════════════════════════════════════════
    # 房客服务
    # ═════════════════════════════════════════
    def _start_guest_services(self) -> None:
        try:
            self._client = ClientConnection(self._peer_ip, self._nickname)
            self._client.joined.connect(self._on_joined)
            self._client.user_list.connect(self._on_user_list)
            self._client.user_joined.connect(self._on_user_joined)
            self._client.user_left.connect(self._on_user_left)
            self._client.frame_received.connect(self._on_frame_received)
            self._client.disconnected.connect(self._on_disconnected)

            self._screen_guest = ScreenGuest()
            self._screen_guest.frame_ready.connect(self._screen_tab.update_frame)
            self._screen_guest.start()

            self._client.connect_to_host()
            self._status_bar.showMessage("正在连接房主...")
        except (ConnectionRefusedError, OSError) as e:
            log.error(TAG, f"Connection failed: {e}")
            QMessageBox.critical(self, "连接失败", f"无法连接房主：{e}")
            sys.exit(1)

    def _on_joined(self, assigned_id: int, nickname: str) -> None:
        self._my_id = assigned_id
        self._nicknames.set(assigned_id, nickname)
        self._chat_tab.setup(assigned_id, self._nicknames)
        self._chat_tab.append_system(f"已加入房间，你的ID是 {assigned_id}")
        self._connect_voice_tcp(assigned_id)

        self._guest_audio = GuestAudio(self._client, assigned_id, self._voice_sock,
            input_device_index=self._voice_tab.get_selected_input(), output_device_index=self._voice_tab.get_selected_output())
        self._guest_audio.open_output()

        def _guest_file_cb(msg_type, target_id, payload):
            if self._client: self._client.send_frame(msg_type, target_id, payload)
            
        self._init_file_manager(_guest_file_cb)
        self._status_bar.showMessage(f"已连接房主 — ID: {assigned_id}")

    def _on_user_list(self, users_list: list) -> None:
        for uid, nick in users_list: self._nicknames.set(uid, nick)
        self._online_users_changed.emit(dict(self._nicknames.get_all()))
        self._update_guest_file_targets()

    def _on_user_joined(self, uid: int, nickname: str) -> None:
        self._nicknames.set(uid, nickname)
        self._chat_tab.append_system(f"{nickname} 加入了房间")
        self._online_users_changed.emit(dict(self._nicknames.get_all()))
        self._update_guest_file_targets()

    def _on_user_left(self, uid: int, nickname: str) -> None:
        self._nicknames.remove(uid)
        self._chat_tab.append_system(f"{nickname} 离开了房间")
        self._online_users_changed.emit(dict(self._nicknames.get_all()))
        self._update_guest_file_targets()

    def _update_guest_file_targets(self) -> None:
        targets = {uid: nick for uid, nick in self._nicknames.get_all().items() if uid != self._my_id}
        self._file_tab.update_targets(targets)

    def _on_disconnected(self) -> None:
        self._status_bar.showMessage("与房主断开连接")
        self._screen_tab.stop_streaming()
        self._close_voice_tcp()
        QMessageBox.warning(self, "连接断开", "与房主的连接已断开，将返回启动界面。")
        self.host_disconnected.emit()
        self.close()

    # ═════════════════════════════════════════
    # 帧处理与消息处理
    # ═════════════════════════════════════════
    def _on_frame_received(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        if msg_type == MSG_TEXT: self._handle_text_guest(sender_id, payload)
        elif msg_type == MSG_COMMAND and len(payload) > 0:
            cmd = payload[0]
            if cmd == CMD_SCREEN_STOP:
                if self._screen_guest:
                    self._screen_guest.stop()
                    self._screen_tab.stop_streaming()
            elif cmd == CMD_SCREEN_START:
                if self._screen_guest:
                    self._screen_tab.start_streaming()
                    self._screen_guest.start()
        elif msg_type == MSG_SCREEN_FRAME:
            if self._screen_guest and self._screen_tab._streaming:
                self._screen_guest.push_frame_data(payload)
        elif msg_type in (MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK, MSG_FILE_CHUNK):
            if self._file_manager: self._file_manager.handle_incoming(msg_type, sender_id, payload)

    def _handle_text(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        broadcast_frame = build_frame(MSG_TEXT, sender_id, BROADCAST_ID, payload)
        self._server.broadcast(broadcast_frame, exclude={sender_id}, msg_type=MSG_TEXT)
        try: self._chat_tab.append_message(sender_id, payload.decode("utf-8"))
        except: pass

    def _handle_text_guest(self, sender_id: int, payload: bytes) -> None:
        try: self._chat_tab.append_message(sender_id, payload.decode("utf-8"))
        except: pass

    def _handle_voice(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        if self._audio_mixer: self._audio_mixer.push_client_audio(sender_id, payload)

    def _send_voice_to_client(self, uid: int, pcm_data: bytes) -> None:
        if self._server:
            with self._server._clients_lock: info = self._server._clients.get(uid)
            if info and info.voice_sock:
                frame = build_frame(MSG_VOICE, HOST_ID, uid, pcm_data)
                try:
                    if info.voice_send_queue.full(): info.voice_send_queue.get_nowait()
                    info.voice_send_queue.put_nowait(frame)
                except: pass

    def _get_online_client_ids(self) -> list[int]:
        if self._server:
            with self._server._clients_lock: return list(self._server._clients.keys())
        return []

    # ═════════════════════════════════════════
    # 文件操作槽
    # ═════════════════════════════════════════
    def _on_file_send(self, file_path: str, target_id: int) -> None:
        # 【体验优化】：发送文件时，如果是房主且正在投屏，自动暂停
        if self._is_host and self._screen_host and self._screen_host.running:
            self._on_toggle_screen()
            self._chat_tab.append_system("为保证传输速度，已自动暂停投屏")
        if self._file_manager: self._file_manager.send_file(file_path, target_id)

    def _on_folder_send(self, folder_path: str, target_id: int) -> None:
        if self._is_host and self._screen_host and self._screen_host.running:
            self._on_toggle_screen()
            self._chat_tab.append_system("为保证传输速度，已自动暂停投屏")
        if self._file_manager: self._file_manager.send_folder(folder_path, target_id)

    def _on_resume_requested(self, task_id: str):
        if self._file_manager: self._file_manager.resume_task(task_id)

    def _on_clear_requested(self, task_id: str):
        if self._file_manager: self._file_manager.clear_task(task_id)

    def _on_file_complete(self, task_id: str, path: str) -> None:
        name = os.path.basename(path) if os.path.sep in path or '/' in path else path
        self._file_tab.set_status(f"传输完成: {name}")
        self._file_tab.update_progress(task_id, 100, "", "已完成")

    # ═════════════════════════════════════════
    # 其他事件处理 (语音/投屏/UI等)
    # ═════════════════════════════════════════
    def _on_client_event_ui(self, text: str) -> None:
        self._chat_tab.append_system(text)
        if self._server: self._status_bar.showMessage(f"{text} — 当前 {len(self._server.clients)} 人在线")

    def _on_targets_changed_ui(self, targets_list: list) -> None:
        self._file_tab.update_targets(dict(targets_list))

    def _connect_voice_tcp(self, assigned_id: int) -> None:
        try:
            self._voice_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._voice_sock.settimeout(5.0)
            self._voice_sock.connect((self._peer_ip, config.VOICE_TCP_PORT))
            self._voice_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._voice_sock.settimeout(None)
            self._voice_sock.sendall(struct.pack("!I", assigned_id))
            if not self._voice_sock.recv(1): raise ConnectionError()
            self._voice_recv_thread = threading.Thread(target=self._voice_recv_loop, daemon=True)
            self._voice_recv_thread.start()
        except Exception as e:
            log.error(TAG, f"Voice TCP failed: {e}")
            self._voice_sock = None

    def _voice_recv_loop(self) -> None:
        from core.protocol import read_frame as proto_read_frame
        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = self._voice_sock.recv(n - len(buf))
                if not chunk: raise ConnectionError()
                buf.extend(chunk)
            return bytes(buf)
        try:
            while self._voice_sock:
                result = proto_read_frame(recv_exact)
                if not result: break
                if result[0] == MSG_VOICE: self._voice_data_received.emit(result[3])
        except: pass

    def _on_voice_data_received(self, pcm_data: bytes) -> None:
        if self._guest_audio: self._guest_audio.play_mixed_audio(pcm_data)

    def _close_voice_tcp(self) -> None:
        if self._voice_sock:
            try: self._voice_sock.close()
            except: pass
            self._voice_sock = None
        if self._voice_recv_thread: self._voice_recv_thread.join(timeout=2)

    def _on_toggle_screen(self) -> None:
        if not self._is_host: return
        if self._screen_host and self._screen_host.running:
            self._screen_host.stop()
            self._screen_tab.stop_streaming()
            if self._server: self._server.broadcast(build_frame(MSG_COMMAND, HOST_ID, BROADCAST_ID, bytes([CMD_SCREEN_STOP])), msg_type=MSG_COMMAND)
        else:
            if self._screen_host:
                self._screen_host.start()
                self._screen_tab.start_streaming()
                if self._server: self._server.broadcast(build_frame(MSG_COMMAND, HOST_ID, BROADCAST_ID, bytes([CMD_SCREEN_START])), msg_type=MSG_COMMAND)

    def _on_resolution_changed(self, w: int, h: int) -> None:
        if self._screen_host: self._screen_host.set_resolution(w, h)
    def _on_fps_changed(self, fps: int) -> None:
        if self._screen_host: self._screen_host.set_fps(fps)

    def _on_toggle_mic(self) -> None:
        if self._is_host:
            if self._host_mic_running:
                self._host_mic_running = False
                self._voice_tab.set_mic_off()
                if self._host_mic_thread: self._host_mic_thread.join(timeout=3)
            else:
                if not self._audio_mixer: return
                self._host_mic_running = True
                self._voice_tab.set_mic_on()
                self._host_mic_thread = threading.Thread(target=self._host_mic_loop, daemon=True)
                self._host_mic_thread.start()
        else:
            if not self._guest_audio: return
            if self._guest_audio.mic_on:
                self._guest_audio.stop_mic()
                self._voice_tab.set_mic_off()
            else:
                if self._guest_audio.start_mic(): self._voice_tab.set_mic_on()

    def _host_mic_loop(self) -> None:
        import pyaudio
        pa = stream = None
        try:
            pa = pyaudio.PyAudio()
            kwargs = dict(format=pyaudio.paInt16, channels=config.AUDIO_CHANNELS, input=True, frames_per_buffer=config.AUDIO_CHUNK)
            if self._host_input_device_index >= 0: kwargs['input_device_index'] = self._host_input_device_index
            for rate in [config.AUDIO_RATE, 44100, 48000]:
                try:
                    kwargs['rate'] = rate
                    stream = pa.open(**kwargs)
                    break
                except: stream = None
            if not stream: raise RuntimeError("Mic failed")
            while self._host_mic_running:
                pcm = stream.read(config.AUDIO_CHUNK, exception_on_overflow=False)
                if self._audio_mixer: self._audio_mixer.push_host_audio(pcm)
        except Exception as e:
            self._host_mic_running = False
            self._mic_error_signal.emit(str(e))
        finally:
            if stream: stream.close()
            if pa: pa.terminate()

    def _on_chat_send(self, text: str) -> None:
        data = text.encode("utf-8")
        if self._is_host and self._server: self._server.broadcast(build_frame(MSG_TEXT, HOST_ID, BROADCAST_ID, data), msg_type=MSG_TEXT)
        elif self._client: self._client.send_frame(MSG_TEXT, HOST_ID, data)

    def _on_probe(self) -> None:
        self._status_bar.showMessage("房主在线" if network.probe_tcp(self._peer_ip, config.TCP_PORT) else "房主离线")

    def _on_mic_error(self, error_msg: str) -> None:
        self._voice_tab.set_mic_off()
        self._host_mic_running = False
        QMessageBox.warning(self, "麦克风错误", error_msg)

    def _on_volume_gain_changed(self, gain_percent: int) -> None:
        if self._guest_audio: self._guest_audio.set_volume_gain(gain_percent)
        if self._audio_mixer: self._audio_mixer.set_volume_gain(gain_percent)

    def _populate_audio_devices(self) -> None:
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            ins, outs = [], []
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                name = info.get("name", f"设备 {i}").encode("utf-8", errors="ignore").decode("utf-8")
                if info.get("maxInputChannels", 0) > 0: ins.append((name, i))
                if info.get("maxOutputChannels", 0) > 0: outs.append((name, i))
            pa.terminate()
            self._voice_tab.set_devices(ins, outs)
            if self._is_host: self._screen_tab.set_speakers(SystemAudioCapture.get_speaker_list())
        except: pass

    def _on_voice_device_changed(self, input_idx: int, output_idx: int) -> None:
        self._host_input_device_index = input_idx
        self._host_output_device_index = output_idx
        if self._guest_audio: self._guest_audio.set_device(input_idx, output_idx)
        if self._audio_mixer: self._audio_mixer.set_device(input_idx, output_idx)
        if self._is_host and self._host_mic_running:
            self._host_mic_running = False
            if self._host_mic_thread: self._host_mic_thread.join(timeout=3)
            self._host_mic_running = True
            self._host_mic_thread = threading.Thread(target=self._host_mic_loop, daemon=True)
            self._host_mic_thread.start()

    def _on_share_audio_toggled(self, enabled: bool) -> None:
        if enabled: self._start_system_audio_capture()
        else: self._stop_system_audio_capture()

    def _on_speaker_changed(self, speaker_name: str) -> None:
        if self._system_audio_capture and self._screen_tab.is_share_audio_enabled():
            self._stop_system_audio_capture()
            self._start_system_audio_capture()

    def _start_system_audio_capture(self) -> None:
        if not self._audio_mixer: return
        if not self._system_audio_capture:
            self._system_audio_capture = SystemAudioCapture(push_callback=self._audio_mixer.push_system_audio)
        self._system_audio_capture.start(device_name=self._screen_tab.get_selected_speaker() or None)

    def _stop_system_audio_capture(self) -> None:
        if self._system_audio_capture: self._system_audio_capture.stop()

    def _update_online_panel(self) -> None:
        self._online_panel.update_users(self._nicknames.get_all())

    def _on_online_refresh_tick(self) -> None:
        self._online_panel.update_users(self._nicknames.get_all())

    def _on_panel_collapsed_changed(self, collapsed: bool) -> None:
        self._btn_expand_panel.setVisible(collapsed)
        if collapsed: self._btn_expand_panel.raise_()

    def closeEvent(self, event) -> None:
        self._online_refresh_timer.stop()
        if self._screen_host: self._screen_host.stop()
        if self._screen_guest: self._screen_guest.stop()
        if self._system_audio_capture: self._system_audio_capture.stop()
        if self._audio_mixer: self._audio_mixer.stop()
        if self._guest_audio: self._guest_audio.stop()
        self._host_mic_running = False
        if self._host_mic_thread: self._host_mic_thread.join(timeout=3)
        self._close_voice_tcp()
        if self._file_manager: self._file_manager.cleanup()
        if self._server: self._server.stop()
        if self._client: self._client.stop()
        event.accept()
