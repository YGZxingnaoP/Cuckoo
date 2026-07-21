# -*- coding: utf-8 -*-
"""
星型拓扑 —— 房客端连接
职责：
  1. 连接房主 TCP 端口
  2. 接收线程解析帧并通过 Qt Signal 分发
  3. 发送队列 + 发送线程
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
    CMD_JOIN, CMD_JOIN_ACK, CMD_LEAVE, CMD_USER_LIST,
    HOST_ID, BROADCAST_ID
)
import config

TAG = "ClientConn"


class ClientConnection(QObject):
    """
    房客端 TCP 连接管理。
    信号：
      - frame_received(int, int, int, bytes): (msg_type, sender_id, target_id, payload)
      - joined(int, str): (assigned_id, nickname)
      - user_list(dict): {uid: nickname}
      - user_joined(int, str): (uid, nickname)
      - user_left(int, str): (uid, nickname)
      - disconnected(): 连接断开
    """

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
        self._send_queue: queue.Queue = queue.Queue(maxsize=config.SEND_QUEUE_MAX * 3)  # 有界队列，防止内存无限增长
        self._receiver_thread: Optional[threading.Thread] = None
        self._sender_thread: Optional[threading.Thread] = None
        self._disconnected_emitted = False  # 防止重复发射断开信号

    # ═════════════════════════════════════════
    # 公开属性
    # ═════════════════════════════════════════

    @property
    def my_id(self) -> int:
        return self._my_id

    @property
    def connected(self) -> bool:
        return self._running and self._sock is not None

    # ═════════════════════════════════════════
    # 连接
    # ═════════════════════════════════════════

    def connect_to_host(self) -> None:
        """连接房主并发送 JOIN 信令。"""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5.0)
        self._sock.connect((self._host_ip, config.TCP_PORT))
        self._sock.settimeout(None)
        self._running = True
        self._disconnected_emitted = False
        log.log(TAG, f"Connected to host at {self._host_ip}:{config.TCP_PORT}")

        # 启动发送线程
        self._sender_thread = threading.Thread(
            target=self._send_loop, daemon=True, name="ClientSender"
        )
        self._sender_thread.start()

        # 启动接收线程
        self._receiver_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="ClientReceiver"
        )
        self._receiver_thread.start()

        # 发送 JOIN
        join_data = bytes([CMD_JOIN]) + self._nickname.encode("utf-8")
        join_frame = build_frame(MSG_COMMAND, 0, HOST_ID, join_data)
        self._send_queue.put(join_frame)

    # ═════════════════════════════════════════
    # 接收与发送（内部线程）
    # ═════════════════════════════════════════

    def _recv_loop(self) -> None:
        """接收线程：解析帧并分发。"""
        log.log(TAG, "Receiver thread started")

        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = self._sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("Connection closed by host")
                buf.extend(chunk)
            return bytes(buf)

        try:
            while self._running:
                result = read_frame(recv_exact)
                if result is None:
                    break

                msg_type, sender_id, target_id, payload = result
                log.debug(TAG, f"type=0x{msg_type:02x} from={sender_id} target={target_id}")

                # 处理控制信令
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

                # 其他帧通过信号分发给 UI
                self.frame_received.emit(msg_type, sender_id, target_id, payload)

        except (ConnectionError, OSError) as e:
            log.error(TAG, f"Connection lost: {e}")
            if not self._disconnected_emitted:
                self._disconnected_emitted = True
                self.disconnected.emit()
        finally:
            log.log(TAG, "Receiver thread stopped")

    def _send_loop(self) -> None:
        """发送线程：从队列取帧并发送。"""
        log.log(TAG, "Sender thread started")
        while self._running:
            try:
                frame_bytes = self._send_queue.get(timeout=1.0)
                if frame_bytes is None:
                    break
                self._sock.sendall(frame_bytes)
            except queue.Empty:
                continue
            except (ConnectionError, OSError) as e:
                log.error(TAG, f"Send error: {e}")
                break
        log.log(TAG, "Sender thread stopped")

    # ═════════════════════════════════════════
    # 信令处理
    # ═════════════════════════════════════════

    def _handle_join_ack(self, payload: bytes) -> None:
        """处理 JOIN_ACK：解析分配的 ID。"""
        # payload: [1B cmd][4B assigned_id][nickname]
        self._my_id = struct.unpack("!I", payload[1:5])[0]
        nickname = payload[5:].decode("utf-8", errors="replace") if len(payload) > 5 else self._nickname
        log.log(TAG, f"Joined as ID={self._my_id}, nickname='{nickname}'")
        self.joined.emit(self._my_id, nickname)

    def _handle_user_list(self, payload: bytes) -> None:
        """处理 USER_LIST：解析在线用户列表。"""
        # payload: [1B cmd][1B count][4B uid][1B nick_len][nick]...
        if len(payload) < 2:
            return
        count = payload[1]
        offset = 2
        users = {}
        for _ in range(count):
            if offset + 5 > len(payload):
                break
            uid = struct.unpack("!I", payload[offset:offset + 4])[0]
            offset += 4
            nick_len = payload[offset]
            offset += 1
            nick = payload[offset:offset + nick_len].decode("utf-8", errors="replace")
            offset += nick_len
            users[uid] = nick
        log.log(TAG, f"User list: {users}")
        self.user_list.emit(list(users.items()))

    # ═════════════════════════════════════════
    # 发送 API
    # ═════════════════════════════════════════

    def send_frame(self, msg_type: int, target_id: int, payload: bytes = b"") -> None:
        """构建并发送一帧。"""
        frame = build_frame(msg_type, self._my_id, target_id, payload)
        self._send_queue.put(frame)

    # ═════════════════════════════════════════
    # 停止
    # ═════════════════════════════════════════

    def stop(self) -> None:
        """断开连接并释放资源。"""
        self._running = False
        self._send_queue.put(None)  # 哨兵

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        if self._sender_thread:
            self._sender_thread.join(timeout=2)
        if self._receiver_thread:
            self._receiver_thread.join(timeout=2)

        log.log(TAG, "Client connection closed")
