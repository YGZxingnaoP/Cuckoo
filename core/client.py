# -*- coding: utf-8 -*-
"""
星型拓扑 —— 房客端连接
"""

import socket
import struct
import threading
import queue
from typing import Optional

from PySide6.QtCore import QObject, Signal

from common import logger as log
from core.protocol import (
    build_frame, read_frame,
    MSG_TEXT, MSG_COMMAND, MSG_SCREEN_FRAME, MSG_FILE_META, MSG_FILE_CHUNK,
    MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK,  # 【修复】补充缺失的导入
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

    def __init__(self, host_ip: str, nickname: str = "房客"):
        super().__init__()
        self._host_ip = host_ip
        self._nickname = nickname
        self._my_id: int = -1
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._priority_queue: queue.Queue = queue.Queue(maxsize=config.SEND_QUEUE_MAX * 3)  # 信令与文件
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
        log.log(TAG, "Receiver thread started")
        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = self._sock.recv(n - len(buf))
                if not chunk: raise ConnectionError("Connection closed by host")
                buf.extend(chunk)
            return bytes(buf)

        try:
            while self._running:
                result = read_frame(recv_exact)
                if result is None: break
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
            log.error(TAG, f"Connection lost: {e}")
            if not self._disconnected_emitted:
                self._disconnected_emitted = True
                self.disconnected.emit()
        finally:
            log.log(TAG, "Receiver thread stopped")

    def _send_loop(self) -> None:
        log.log(TAG, "Sender thread started")
        while self._running:
            frame_bytes = None
            try:
                # 1. 最高优先级：信令与文件（阻塞获取，绝不丢弃）
                try:
                    frame_bytes = self._priority_queue.get_nowait()
                except queue.Empty:
                    pass

                # 2. 最低优先级：投屏帧（清空旧帧，只发最新）
                if frame_bytes is None:
                    try:
                        while True:
                            frame_bytes = self._media_queue.get_nowait()
                    except queue.Empty:
                        pass

                # 3. 如果都没有，阻塞等待信令队列
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
        """构建并发送一帧。信令/文件走优先级队列，投屏帧走媒体队列。"""
        frame = build_frame(msg_type, self._my_id, target_id, payload)

        if msg_type == MSG_SCREEN_FRAME:
            # 投屏帧：非阻塞，满了就丢弃旧的
            try:
                if self._media_queue.full():
                    self._media_queue.get_nowait()
                self._media_queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass
        else:
            # 文件与信令：阻塞入队，保证送达
            try:
                self._priority_queue.put(frame, timeout=5.0)
            except queue.Full:
                log.warn(TAG, f"Priority queue full, dropping: {msg_type}")

    def stop(self) -> None:
        self._running = False
        self._priority_queue.put(False)
        if self._sock:
            try: self._sock.close()
            except OSError: pass
            self._sock = None
        if self._sender_thread: self._sender_thread.join(timeout=2)
        if self._receiver_thread: self._receiver_thread.join(timeout=2)
        log.log(TAG, "Client connection closed")
