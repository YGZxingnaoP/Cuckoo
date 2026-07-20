# -*- coding: utf-8 -*-
"""
星型拓扑 —— 房主端服务器
职责：
  1. 监听 TCP 端口，接受房客连接
  2. 每连接一个接收线程 + 每客户端独立发送队列
  3. 路由帧：文本广播、文件中转、投屏分发、信令处理
"""

import socket
import struct
import threading
import queue
from dataclasses import dataclass, field
from typing import Optional, Callable

from common import logger as log
from core.protocol import (
    build_frame, read_frame, BROADCAST_ID, HOST_ID,
    MSG_TEXT, MSG_FILE_META, MSG_FILE_CHUNK, MSG_SCREEN_FRAME, MSG_COMMAND,
    CMD_JOIN, CMD_JOIN_ACK, CMD_LEAVE, CMD_USER_LIST
)
import config

TAG = "Server"


# ─────────────────────────────────────────────
# 客户端信息结构
# ─────────────────────────────────────────────
@dataclass
class ClientInfo:
    uid: int
    sock: socket.socket
    addr: tuple
    nickname: str = ""
    send_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=config.SEND_QUEUE_MAX))
    sender_thread: Optional[threading.Thread] = None
    receiver_thread: Optional[threading.Thread] = None


# ─────────────────────────────────────────────
# 服务器主类
# ─────────────────────────────────────────────
class Server:
    """
    星型拓扑服务端（房主运行）。
    - 接受 TCP 连接，分配 ID
    - 路由帧（文本广播、文件中转、投屏分发）
    - 管理客户端生命周期
    """

    def __init__(self, host_nickname: str = "房主"):
        self._host_nickname = host_nickname
        self._server_sock: Optional[socket.socket] = None
        self._running = False
        self._clients_lock = threading.Lock()
        self._clients: dict[int, ClientInfo] = {}
        self._next_id = 1

        # 消息处理器注册表: msg_type -> callback(msg_type, sender_id, target_id, payload)
        self._handlers: dict[int, Callable] = {}

        # 外部事件回调
        self._on_client_joined: Optional[Callable[[int, str], None]] = None
        self._on_client_left: Optional[Callable[[int, str], None]] = None

    # ═════════════════════════════════════════
    # 公开属性
    # ═════════════════════════════════════════

    @property
    def clients(self) -> dict[int, ClientInfo]:
        with self._clients_lock:
            return dict(self._clients)

    @property
    def running(self) -> bool:
        return self._running

    def set_on_client_joined(self, cb: Callable[[int, str], None]) -> None:
        self._on_client_joined = cb

    def set_on_client_left(self, cb: Callable[[int, str], None]) -> None:
        self._on_client_left = cb

    def register_handler(self, msg_type: int, handler: Callable) -> None:
        """注册消息类型处理器。"""
        self._handlers[msg_type] = handler

    # ═════════════════════════════════════════
    # 启动与停止
    # ═════════════════════════════════════════

    def start(self, bind_addr: str = "0.0.0.0", port: int = config.TCP_PORT) -> None:
        """启动 TCP 服务器。"""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((bind_addr, port))
        self._server_sock.listen(config.MAX_CLIENTS)
        self._server_sock.settimeout(config.ACCEPT_TIMEOUT)
        self._running = True
        log.log(TAG, f"Server listening on {bind_addr}:{port}")

        accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="ServerAccept"
        )
        accept_thread.start()

    def stop(self) -> None:
        """停止服务器，断开所有客户端。"""
        self._running = False

        # 关闭所有客户端
        with self._clients_lock:
            for info in list(self._clients.values()):
                try:
                    info.sock.close()
                except OSError:
                    pass
            self._clients.clear()

        # 关闭服务器 socket
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None

        log.log(TAG, "Server stopped")

    # ═════════════════════════════════════════
    # 连接管理（内部）
    # ═════════════════════════════════════════

    def _accept_loop(self) -> None:
        """Accept 线程：等待客户端连接。"""
        log.log(TAG, "Accept loop started")
        while self._running:
            try:
                client_sock, addr = self._server_sock.accept()
                client_sock.settimeout(None)
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    log.error(TAG, "Accept error")
                break

            # 分配 ID
            with self._clients_lock:
                if self._next_id > config.MAX_CLIENTS:
                    log.warn(TAG, f"Max clients reached, rejecting {addr}")
                    client_sock.close()
                    continue
                uid = self._next_id
                self._next_id += 1

            info = ClientInfo(uid=uid, sock=client_sock, addr=addr)
            with self._clients_lock:
                self._clients[uid] = info

            log.log(TAG, f"Client {uid} connected from {addr}")

            # 启动发送线程
            sender = threading.Thread(
                target=self._sender_loop, args=(info,),
                daemon=True, name=f"ClientSender-{uid}"
            )
            info.sender_thread = sender
            sender.start()

            # 启动接收线程
            receiver = threading.Thread(
                target=self._receiver_loop, args=(info,),
                daemon=True, name=f"ClientReceiver-{uid}"
            )
            info.receiver_thread = receiver
            receiver.start()

        log.log(TAG, "Accept loop stopped")

    def _sender_loop(self, info: ClientInfo) -> None:
        """每客户端发送线程：从队列取帧并发送。"""
        log.log(TAG, f"Sender started for client {info.uid}")
        while self._running:
            try:
                frame_bytes = info.send_queue.get(timeout=1.0)
                if frame_bytes is None:  # 哨兵值，退出
                    break
                # 背压处理：如果队列积压过多，丢弃旧帧
                info.sock.sendall(frame_bytes)
            except queue.Empty:
                continue
            except (ConnectionError, OSError) as e:
                log.error(TAG, f"Send error for client {info.uid}: {e}")
                break
        log.log(TAG, f"Sender stopped for client {info.uid}")

    def _receiver_loop(self, info: ClientInfo) -> None:
        """每客户端接收线程：读取帧并路由。"""
        log.log(TAG, f"Receiver started for client {info.uid}")

        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = info.sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("Connection closed")
                buf.extend(chunk)
            return bytes(buf)

        try:
            while self._running:
                result = read_frame(recv_exact)
                if result is None:
                    break

                msg_type, sender_id, target_id, payload = result
                # 覆盖 sender_id 为实际连接的 ID（防止伪造）
                sender_id = info.uid

                log.debug(TAG, f"[C{info.uid}] type=0x{msg_type:02x} target={target_id}")

                # 处理 JOIN 信令
                if msg_type == MSG_COMMAND and len(payload) > 0 and payload[0] == CMD_JOIN:
                    self._handle_join(info, payload)
                    continue

                # 调用注册的处理器
                if msg_type in self._handlers:
                    self._handlers[msg_type](msg_type, sender_id, target_id, payload)
                    continue

                # 默认路由：根据 target_id 转发
                self._route_frame(msg_type, sender_id, target_id, payload)

        except (ConnectionError, OSError) as e:
            log.error(TAG, f"Client {info.uid} disconnected: {e}")
        finally:
            self._remove_client(info.uid)
            log.log(TAG, f"Receiver stopped for client {info.uid}")

    def _handle_join(self, info: ClientInfo, payload: bytes) -> None:
        """处理 JOIN 信令：提取昵称，分配 ID，广播用户列表。"""
        nickname = ""
        if len(payload) > 1:
            try:
                nickname = payload[1:].decode("utf-8")
            except UnicodeDecodeError:
                nickname = f"房客{info.uid}"
        else:
            nickname = f"房客{info.uid}"

        info.nickname = nickname
        log.log(TAG, f"Client {info.uid} joined as '{nickname}'")

        # 发送 JOIN_ACK: [1B cmd][4B assigned_id][nickname]
        ack_data = bytes([CMD_JOIN_ACK]) + struct.pack("!I", info.uid) + nickname.encode("utf-8")
        ack_frame = build_frame(MSG_COMMAND, HOST_ID, info.uid, ack_data)
        self._queue_bytes(info.uid, ack_frame)

        # 发送当前用户列表
        self._send_user_list(info.uid)

        # 广播新用户加入给其他客户端
        join_notify = build_frame(
            MSG_COMMAND, info.uid, BROADCAST_ID,
            bytes([CMD_JOIN]) + nickname.encode("utf-8")
        )
        self.broadcast(join_notify, exclude={info.uid})

        # 通知外部
        if self._on_client_joined:
            self._on_client_joined(info.uid, nickname)

    def _send_user_list(self, target_uid: int) -> None:
        """向指定客户端发送当前在线用户列表。"""
        # 格式: [count][4B uid1][nick_len1][nick1]...
        with self._clients_lock:
            all_users = [(HOST_ID, self._host_nickname)]
            for cid, ci in self._clients.items():
                if ci.nickname:
                    all_users.append((cid, ci.nickname))

        data = bytearray([CMD_USER_LIST, len(all_users)])
        for uid, nick in all_users:
            nick_bytes = nick.encode("utf-8")
            data.extend(struct.pack("!I", uid))
            data.append(len(nick_bytes))
            data.extend(nick_bytes)

        frame = build_frame(MSG_COMMAND, HOST_ID, target_uid, bytes(data))
        self._queue_bytes(target_uid, frame)

    # ═════════════════════════════════════════
    # 帧路由
    # ═════════════════════════════════════════

    def _route_frame(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        """根据 target_id 路由帧。"""
        frame = build_frame(msg_type, sender_id, target_id, payload)
        if target_id == BROADCAST_ID:
            self.broadcast(frame, exclude={sender_id})
        elif target_id == HOST_ID:
            pass  # 已在 handler 中处理或忽略
        else:
            self._queue_bytes(target_id, frame)

    # ═════════════════════════════════════════
    # 发送 API
    # ═════════════════════════════════════════

    def send_to(self, uid: int, frame_bytes: bytes) -> None:
        """向指定客户端排队发送帧。"""
        self._queue_bytes(uid, frame_bytes)

    def broadcast(self, frame_bytes: bytes, exclude: set = None) -> None:
        """广播帧给所有客户端（可排除指定 ID）。"""
        exclude = exclude or set()
        # 取快照以避免持锁时间过长
        with self._clients_lock:
            targets = list(self._clients.values())
        for info in targets:
            if info.uid not in exclude:
                self._queue_bytes(info.uid, frame_bytes)

    def broadcast_from(self, sender_id: int, msg_type: int, payload: bytes) -> None:
        """将某用户的消息广播给其他所有用户（含房主自身由 handler 处理）。"""
        frame = build_frame(msg_type, sender_id, BROADCAST_ID, payload)
        self.broadcast(frame, exclude={sender_id})

    def _queue_bytes(self, uid: int, frame_bytes: bytes) -> None:
        """向指定客户端的发送队列放入数据。"""
        with self._clients_lock:
            info = self._clients.get(uid)
        if info is None:
            return
        try:
            if info.send_queue.full():
                try:
                    info.send_queue.get_nowait()  # 丢弃旧帧
                except queue.Empty:
                    pass
            info.send_queue.put_nowait(frame_bytes)
        except queue.Full:
            pass

    # ═════════════════════════════════════════
    # 客户端清理
    # ═════════════════════════════════════════

    def _remove_client(self, uid: int) -> None:
        """清理断开连接的客户端。"""
        with self._clients_lock:
            info = self._clients.pop(uid, None)

        if info is None:
            return

        nickname = info.nickname or f"房客{uid}"
        log.log(TAG, f"Removing client {uid} ({nickname})")

        try:
            info.sock.close()
        except OSError:
            pass

        # 停止发送线程
        try:
            info.send_queue.put_nowait(None)
        except queue.Full:
            pass

        # 广播离开通知
        leave_data = bytes([CMD_LEAVE]) + nickname.encode("utf-8")
        leave_frame = build_frame(MSG_COMMAND, uid, BROADCAST_ID, leave_data)
        self.broadcast(leave_frame)

        # 通知外部
        if self._on_client_left:
            self._on_client_left(uid, nickname)
