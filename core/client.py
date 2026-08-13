# -*- coding: utf-8 -*-
"""
星型拓扑 —— 房客端连接
"""

import socket
import struct
import threading
import time
import queue
from typing import Optional

from PySide6.QtCore import QObject, Signal

from common import logger as log
from core.protocol import (
    build_frame, read_frame,
    MSG_TEXT, MSG_COMMAND, MSG_SCREEN_FRAME, MSG_FILE_META, MSG_FILE_CHUNK,
    MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ,
    MSG_CINEMA_CMD, MSG_FILE_CHUNK_ACK,
    MSG_FILE_OFFER, MSG_FILE_OFFER_RESP, MSG_FILE_CANCEL,
    MSG_FILE_RETRANSMIT_REQ, MSG_FILE_VERIFY,
    CMD_JOIN, CMD_JOIN_ACK, CMD_LEAVE, CMD_USER_LIST,
    HOST_ID, BROADCAST_ID
)
import config

TAG = "ClientConn"

class ClientConnection(QObject):
    frame_received = Signal(int, int, int, bytes)
    joined = Signal(int, str)
    user_list = Signal(list)
    user_joined = Signal(int, str)
    user_left = Signal(int, str)
    disconnected = Signal()
    reconnected = Signal()  # 【新增】重连成功信号，通知UI重建语音通道等

    def __init__(self, host_ip: str, nickname: str = "房客"):
        super().__init__()
        self._host_ip = host_ip
        self._nickname = nickname
        self._my_id: int = -1
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._priority_queue: queue.Queue = queue.Queue(maxsize=config.SEND_QUEUE_MAX * 3)  # 信令队列
        self._file_queue: queue.Queue = queue.Queue(maxsize=config.FILE_QUEUE_MAX)           # 【P0修复】独立文件队列
        self._media_queue: queue.Queue = queue.Queue(maxsize=config.SEND_QUEUE_MAX)          # 投屏帧
        self._receiver_thread: Optional[threading.Thread] = None
        self._sender_thread: Optional[threading.Thread] = None
        self._disconnected_emitted = False

    @property
    def my_id(self) -> int: return self._my_id

    @property
    def connected(self) -> bool: return self._running and self._sock is not None

    def connect_to_host(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5.0)
        self._sock.connect((self._host_ip, config.TCP_PORT))
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(None)
        self._running = True
        self._disconnected_emitted = False
        log.log(TAG, f"Connected to host at {self._host_ip}:{config.TCP_PORT}")

        self._sender_thread = threading.Thread(target=self._send_loop, daemon=True, name="ClientSender")
        self._sender_thread.start()
        self._receiver_thread = threading.Thread(target=self._recv_loop, daemon=True, name="ClientReceiver")
        self._receiver_thread.start()

        join_data = bytes([CMD_JOIN]) + self._nickname.encode("utf-8")
        join_frame = build_frame(MSG_COMMAND, 0, HOST_ID, join_data)
        self._priority_queue.put(join_frame)

    def _recv_loop(self) -> None:
        """接收循环（首次连接和重连后共用）"""
        log.log(TAG, "Receiver thread started")
        self._run_recv_loop()
        log.log(TAG, "Receiver thread stopped")

    def _run_recv_loop(self) -> None:
        """接收循环核心逻辑"""
        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = self._sock.recv(n - len(buf))
                if not chunk: raise ConnectionError("Connection closed by host")
                buf.extend(chunk)
            return bytes(buf)

        while self._running:
            try:
                result = read_frame(recv_exact)
                if result is None:
                    break
                msg_type, sender_id, target_id, payload = result

                if msg_type == MSG_COMMAND and len(payload) > 0:
                    cmd = payload[0]
                    if cmd == CMD_JOIN_ACK:
                        self._handle_join_ack(payload)
                        continue
                    elif cmd == CMD_USER_LIST:
                        self._handle_user_list(payload)
                        continue
                    elif cmd == CMD_JOIN:
                        nick = payload[1:].decode("utf-8", errors="replace")
                        self.user_joined.emit(sender_id, nick)
                        continue
                    elif cmd == CMD_LEAVE:
                        nick = payload[1:].decode("utf-8", errors="replace")
                        self.user_left.emit(sender_id, nick)
                        continue

                self.frame_received.emit(msg_type, sender_id, target_id, payload)

            except (ConnectionError, OSError) as e:
                if not self._running:
                    break
                log.warn(TAG, f"Connection lost: {e}, attempting reconnect...")
                if self._try_reconnect():
                    log.log(TAG, "Reconnection successful, resuming receiver")
                    continue  # 重新进入接收循环
                elif not self._disconnected_emitted:
                    self._disconnected_emitted = True
                    self.disconnected.emit()
                break

    def _try_reconnect(self) -> bool:
        """尝试重连，返回是否成功"""
        for attempt in range(config.RECONNECT_MAX_RETRY):
            log.log(TAG, f"Reconnect attempt {attempt + 1}/{config.RECONNECT_MAX_RETRY}...")
            time.sleep(config.RECONNECT_INTERVAL)

            if not self._running:
                return False

            try:
                # 关闭旧socket
                if self._sock:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None

                # 创建新连接
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.settimeout(5.0)
                self._sock.connect((self._host_ip, config.TCP_PORT))
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock.settimeout(None)

                log.log(TAG, f"Reconnected to {self._host_ip}:{config.TCP_PORT}")

                # 重启发送线程
                if self._sender_thread and self._sender_thread.is_alive():
                    self._sender_thread = None
                self._sender_thread = threading.Thread(
                    target=self._send_loop, daemon=True, name="ClientSender")
                self._sender_thread.start()

                # 重新发送JOIN
                join_data = bytes([CMD_JOIN]) + self._nickname.encode("utf-8")
                join_frame = build_frame(MSG_COMMAND, 0, HOST_ID, join_data)
                try:
                    self._priority_queue.put_nowait(join_frame)
                except queue.Full:
                    pass

                self.reconnected.emit()
                return True

            except (ConnectionRefusedError, socket.timeout, OSError) as e:
                log.warn(TAG, f"Reconnect attempt {attempt + 1} failed: {e}")

        log.error(TAG, f"All {config.RECONNECT_MAX_RETRY} reconnect attempts failed")
        return False

    def _send_loop(self) -> None:
        log.log(TAG, "Sender thread started")
        while self._running:
            frame_bytes = None
            try:
                # 1. 最高优先级：信令/控制消息
                try:
                    frame_bytes = self._priority_queue.get_nowait()
                except queue.Empty:
                    pass

                # 2. 次高优先级：文件块 (防止被投屏饿死)
                if frame_bytes is None:
                    try:
                        frame_bytes = self._file_queue.get_nowait()
                    except queue.Empty:
                        pass

                # 3. 最低优先级：投屏帧 (丢弃旧帧只发最新)
                if frame_bytes is None:
                    try:
                        while True:
                            frame_bytes = self._media_queue.get_nowait()
                    except queue.Empty:
                        pass

                # 4. 阻塞等待信令队列
                if frame_bytes is None:
                    try:
                        frame_bytes = self._priority_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue

                if frame_bytes is False: break
                self._sock.sendall(frame_bytes)
            except (ConnectionError, OSError) as e:
                log.error(TAG, f"Send error: {e}")
                break
        log.log(TAG, "Sender thread stopped")

    def _handle_join_ack(self, payload: bytes) -> None:
        self._my_id = struct.unpack("!I", payload[1:5])[0]
        nickname = payload[5:].decode("utf-8", errors="replace") if len(payload) > 5 else self._nickname
        log.log(TAG, f"Joined as ID={self._my_id}, nickname='{nickname}'")
        self.joined.emit(self._my_id, nickname)

    def _handle_user_list(self, payload: bytes) -> None:
        if len(payload) < 2: return
        count = payload[1]
        offset = 2
        users = {}
        for _ in range(count):
            if offset + 5 > len(payload): break
            uid = struct.unpack("!I", payload[offset:offset + 4])[0]
            offset += 4
            nick_len = payload[offset]
            offset += 1
            nick = payload[offset:offset + nick_len].decode("utf-8", errors="replace")
            offset += nick_len
            users[uid] = nick
        self.user_list.emit(list(users.items()))

    def send_frame(self, msg_type: int, target_id: int, payload: bytes = b"") -> None:
        """构建并发送一帧。信令走优先级队列，文件数据走文件队列，投屏帧走媒体队列。
        
        【稳定优先】MSG_FILE_CHUNK 数据块阻塞入队（正确背压，绝不丢块）；
        文件控制消息（offer/meta/ack 等）非阻塞入队（小体积，可能被 UI 线程调用）。
        """
        frame = build_frame(msg_type, self._my_id, target_id, payload)

        # ── 文件控制消息（小体积，非阻塞） ──
        _FILE_CTRL_TYPES = (
            MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ,
            MSG_FILE_CHUNK_ACK, MSG_FILE_OFFER, MSG_FILE_OFFER_RESP,
            MSG_FILE_CANCEL, MSG_FILE_RETRANSMIT_REQ, MSG_FILE_VERIFY,
        )

        if msg_type == MSG_SCREEN_FRAME:
            # 投屏帧：非阻塞，满了就丢弃旧的
            try:
                if self._media_queue.full():
                    self._media_queue.get_nowait()
                self._media_queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass

        elif msg_type == MSG_FILE_CHUNK:
            # 数据块：阻塞入队（正确背压），但定期检查连接是否断开，防止永久死锁
            while self._running:
                try:
                    self._file_queue.put(frame, timeout=5.0)
                    break
                except queue.Full:
                    if not self._running:
                        return
                    # 仍在运行，继续阻塞等待背压解除
            if not self._running:
                return

        elif msg_type in _FILE_CTRL_TYPES:
            # 控制消息：非阻塞（小体积，可能 UI 线程调用，不能卡 UI）
            try:
                self._file_queue.put_nowait(frame)
            except queue.Full:
                log.warn(TAG, f"File ctrl queue full for msg_type={msg_type}, drain media")
                try:
                    self._media_queue.get_nowait()
                    self._file_queue.put_nowait(frame)
                except (queue.Empty, queue.Full):
                    log.error(TAG, f"CRITICAL: file ctrl msg_type={msg_type} dropped")

        elif msg_type == MSG_CINEMA_CMD:
            # 电影院控制命令：不可丢弃
            try:
                self._priority_queue.put(frame, timeout=10.0)
            except queue.Full:
                log.error(TAG, "Priority queue stuck, cinema command lost")

        else:
            # 信令：非阻塞入队
            try:
                if self._priority_queue.full():
                    self._priority_queue.get_nowait()
                self._priority_queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass

    def stop(self) -> None:
        self._running = False
        # 向所有队列发送停止信号
        self._priority_queue.put(False)
        try: self._file_queue.put_nowait(False)
        except queue.Full: pass
        if self._sock:
            try: self._sock.close()
            except OSError: pass
            self._sock = None
        if self._sender_thread: self._sender_thread.join(timeout=2)
        if self._receiver_thread: self._receiver_thread.join(timeout=2)
        log.log(TAG, "Client connection closed")
