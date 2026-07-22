# -*- coding: utf-8 -*-
"""
星型拓扑 —— 房主端服务器
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
    MSG_TEXT, MSG_FILE_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK,
    MSG_FILE_TASK_META, MSG_FILE_CHUNK, MSG_SCREEN_FRAME, MSG_COMMAND, MSG_VOICE,
    CMD_JOIN, CMD_JOIN_ACK, CMD_LEAVE, CMD_USER_LIST
)
import config

TAG = "Server"

@dataclass
class ClientInfo:
    uid: int
    sock: socket.socket
    addr: tuple
    nickname: str = ""
    priority_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=config.SEND_QUEUE_MAX))
    media_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=config.SEND_QUEUE_MAX))
    file_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=config.FILE_QUEUE_MAX))
    sender_thread: Optional[threading.Thread] = None
    receiver_thread: Optional[threading.Thread] = None
    voice_sock: Optional[socket.socket] = None
    voice_send_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=config.SEND_QUEUE_MAX))
    voice_sender_thread: Optional[threading.Thread] = None
    voice_receiver_thread: Optional[threading.Thread] = None

class Server:
    def __init__(self, host_nickname: str = "房主"):
        self._host_nickname = host_nickname
        self._server_sock: Optional[socket.socket] = None
        self._voice_server_sock: Optional[socket.socket] = None
        self._running = False
        self._clients_lock = threading.Lock()
        self._clients: dict[int, ClientInfo] = {}
        self._next_id = 1
        self._handlers: dict[int, Callable] = {}
        self._on_client_joined: Optional[Callable[[int, str], None]] = None
        self._on_client_left: Optional[Callable[[int, str], None]] = None

    @property
    def clients(self) -> dict[int, ClientInfo]:
        with self._clients_lock: return dict(self._clients)

    @property
    def running(self) -> bool: return self._running

    def set_on_client_joined(self, cb: Callable[[int, str], None]) -> None: self._on_client_joined = cb
    def set_on_client_left(self, cb: Callable[[int, str], None]) -> None: self._on_client_left = cb
    def register_handler(self, msg_type: int, handler: Callable) -> None: self._handlers[msg_type] = handler

    def start(self, bind_addr: str = "0.0.0.0", port: int = config.TCP_PORT) -> None:
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((bind_addr, port))
        self._server_sock.listen(config.MAX_CLIENTS)
        self._server_sock.settimeout(config.ACCEPT_TIMEOUT)

        self._voice_server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._voice_server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._voice_server_sock.bind((bind_addr, config.VOICE_TCP_PORT))
        self._voice_server_sock.listen(config.MAX_CLIENTS)
        self._voice_server_sock.settimeout(config.ACCEPT_TIMEOUT)

        self._running = True
        log.log(TAG, f"Server listening on {bind_addr}:{port} (main) + :{config.VOICE_TCP_PORT} (voice)")

        threading.Thread(target=self._accept_loop, daemon=True, name="ServerAccept").start()
        threading.Thread(target=self._voice_accept_loop, daemon=True, name="VoiceAccept").start()

    def stop(self) -> None:
        self._running = False
        with self._clients_lock:
            for info in list(self._clients.values()):
                try: info.sock.close()
                except OSError: pass
                if info.voice_sock:
                    try: info.voice_sock.close()
                    except OSError: pass
            self._clients.clear()

        if self._server_sock:
            try: self._server_sock.close()
            except OSError: pass
            self._server_sock = None
        if self._voice_server_sock:
            try: self._voice_server_sock.close()
            except OSError: pass
            self._voice_server_sock = None
        log.log(TAG, "Server stopped")

    def _accept_loop(self) -> None:
        log.log(TAG, "Accept loop started")
        while self._running:
            try:
                client_sock, addr = self._server_sock.accept()
                # 开启 TCP_NODELAY 禁用 Nagle 算法，降低延迟
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client_sock.settimeout(None)
            except socket.timeout: continue
            except OSError:
                if self._running: log.error(TAG, "Accept error")
                break

            with self._clients_lock:
                if self._next_id > config.MAX_CLIENTS:
                    log.warn(TAG, f"Max clients reached, rejecting {addr}")
                    client_sock.close()
                    continue
                uid = self._next_id
                self._next_id += 1
                info = ClientInfo(uid=uid, sock=client_sock, addr=addr)
                self._clients[uid] = info

            log.log(TAG, f"Client {uid} connected from {addr}")
            
            sender = threading.Thread(target=self._sender_loop, args=(info,), daemon=True, name=f"ClientSender-{uid}")
            info.sender_thread = sender
            sender.start()

            receiver = threading.Thread(target=self._receiver_loop, args=(info,), daemon=True, name=f"ClientReceiver-{uid}")
            info.receiver_thread = receiver
            receiver.start()
        log.log(TAG, "Accept loop stopped")

    def _voice_accept_loop(self) -> None:
        log.log(TAG, "Voice accept loop started")
        while self._running:
            try:
                voice_sock, addr = self._voice_server_sock.accept()
                voice_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                voice_sock.settimeout(None)
            except socket.timeout: continue
            except OSError:
                if self._running: log.error(TAG, "Voice accept error")
                break

            try:
                voice_sock.settimeout(5.0)
                uid_bytes = voice_sock.recv(4)
                if not uid_bytes or len(uid_bytes) < 4:
                    voice_sock.close()
                    continue
                uid = struct.unpack("!I", uid_bytes)[0]
                voice_sock.settimeout(None)
            except Exception as e:
                log.error(TAG, f"Voice uid identification failed: {e}")
                voice_sock.close()
                continue

            with self._clients_lock: info = self._clients.get(uid)
            if info is None:
                log.warn(TAG, f"Voice connection for unknown client {uid}, rejecting")
                voice_sock.close()
                continue

            if info.voice_sock is not None:
                log.warn(TAG, f"Replacing existing voice connection for client {uid}")
                try: info.voice_sock.close()
                except OSError: pass
                try: info.voice_send_queue.put_nowait(None)
                except queue.Full: pass

            info.voice_sock = voice_sock
            log.log(TAG, f"Voice connection established for client {uid} from {addr}")
            try: voice_sock.sendall(b"\x01")
            except OSError: pass

            vs = threading.Thread(target=self._voice_sender_loop, args=(info,), daemon=True, name=f"VoiceSender-{uid}")
            info.voice_sender_thread = vs
            vs.start()

            vr = threading.Thread(target=self._voice_receiver_loop, args=(info,), daemon=True, name=f"VoiceReceiver-{uid}")
            info.voice_receiver_thread = vr
            vr.start()
        log.log(TAG, "Voice accept loop stopped")

    def _voice_sender_loop(self, info: ClientInfo) -> None:
        log.log(TAG, f"Voice sender started for client {info.uid}")
        while self._running:
            try:
                frame_bytes = info.voice_send_queue.get(timeout=1.0)
                if frame_bytes is None: break
                info.voice_sock.sendall(frame_bytes)
            except queue.Empty: continue
            except (ConnectionError, OSError) as e:
                log.error(TAG, f"Voice send error for client {info.uid}: {e}")
                break
        log.log(TAG, f"Voice sender stopped for client {info.uid}")

    def _voice_receiver_loop(self, info: ClientInfo) -> None:
        log.log(TAG, f"Voice receiver started for client {info.uid}")
        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = info.voice_sock.recv(n - len(buf))
                if not chunk: raise ConnectionError("Voice connection closed")
                buf.extend(chunk)
            return bytes(buf)

        try:
            while self._running:
                result = read_frame(recv_exact)
                if result is None: break
                msg_type, sender_id, target_id, payload = result
                sender_id = info.uid
                if msg_type in self._handlers:
                    self._handlers[msg_type](msg_type, sender_id, target_id, payload)
        except (ConnectionError, OSError) as e:
            log.error(TAG, f"Voice receiver error for client {info.uid}: {e}")
        log.log(TAG, f"Voice receiver stopped for client {info.uid}")

    def _sender_loop(self, info: ClientInfo) -> None:
        log.log(TAG, f"Sender started for client {info.uid}")
        while self._running:
            frame_bytes = None
            try:
                try: frame_bytes = info.priority_queue.get_nowait()
                except queue.Empty: pass

                if frame_bytes is None:
                    try:
                        while True: frame_bytes = info.media_queue.get_nowait()
                    except queue.Empty: pass

                if frame_bytes is None:
                    try: frame_bytes = info.file_queue.get_nowait()
                    except queue.Empty: pass

                if frame_bytes is None:
                    try: frame_bytes = info.priority_queue.get(timeout=0.1)
                    except queue.Empty: continue

                if frame_bytes is False: break
                info.sock.sendall(frame_bytes)
            except (ConnectionError, OSError) as e:
                log.error(TAG, f"Send error for client {info.uid}: {e}")
                break
        log.log(TAG, f"Sender stopped for client {info.uid}")

    def _receiver_loop(self, info: ClientInfo) -> None:
        log.log(TAG, f"Receiver started for client {info.uid}")
        def recv_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = info.sock.recv(n - len(buf))
                if not chunk: raise ConnectionError("Connection closed")
                buf.extend(chunk)
            return bytes(buf)

        try:
            while self._running:
                result = read_frame(recv_exact)
                if result is None: break
                msg_type, sender_id, target_id, payload = result
                sender_id = info.uid

                if msg_type == MSG_COMMAND and len(payload) > 0 and payload[0] == CMD_JOIN:
                    self._handle_join(info, payload)
                    continue

                if msg_type in self._handlers:
                    self._handlers[msg_type](msg_type, sender_id, target_id, payload)
                    continue

                self._route_frame(msg_type, sender_id, target_id, payload)
        except (ConnectionError, OSError) as e:
            log.error(TAG, f"Client {info.uid} disconnected: {e}")
        finally:
            self._remove_client(info.uid)
            log.log(TAG, f"Receiver stopped for client {info.uid}")

    def _handle_join(self, info: ClientInfo, payload: bytes) -> None:
        nickname = ""
        if len(payload) > 1:
            try: nickname = payload[1:].decode("utf-8")
            except UnicodeDecodeError: nickname = f"房客{info.uid}"
        else: nickname = f"房客{info.uid}"

        info.nickname = nickname
        log.log(TAG, f"Client {info.uid} joined as '{nickname}'")

        ack_data = bytes([CMD_JOIN_ACK]) + struct.pack("!I", info.uid) + nickname.encode("utf-8")
        ack_frame = build_frame(MSG_COMMAND, HOST_ID, info.uid, ack_data)
        self._queue_bytes(info.uid, ack_frame)
        self._send_user_list(info.uid)

        join_notify = build_frame(MSG_COMMAND, info.uid, BROADCAST_ID, bytes([CMD_JOIN]) + nickname.encode("utf-8"))
        self.broadcast(join_notify, exclude={info.uid})

        if self._on_client_joined: self._on_client_joined(info.uid, nickname)

    def _send_user_list(self, target_uid: int) -> None:
        with self._clients_lock:
            all_users = [(HOST_ID, self._host_nickname)]
            for cid, ci in self._clients.items():
                if ci.nickname: all_users.append((cid, ci.nickname))

        data = bytearray([CMD_USER_LIST, len(all_users)])
        for uid, nick in all_users:
            nick_bytes = nick.encode("utf-8")
            data.extend(struct.pack("!I", uid))
            data.append(len(nick_bytes))
            data.extend(nick_bytes)

        frame = build_frame(MSG_COMMAND, HOST_ID, target_uid, bytes(data))
        self._queue_bytes(target_uid, frame)

    def _route_frame(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        frame = build_frame(msg_type, sender_id, target_id, payload)
        if target_id == BROADCAST_ID: self.broadcast(frame, exclude={sender_id})
        elif target_id == HOST_ID: pass
        else: self._queue_bytes(target_id, frame)

    def send_to(self, uid: int, frame_bytes: bytes, msg_type: int = 0) -> None:
        self._queue_bytes(uid, frame_bytes, msg_type)

    def broadcast(self, frame_bytes: bytes, exclude: set = None, msg_type: int = 0) -> None:
        exclude = exclude or set()
        with self._clients_lock: targets = list(self._clients.values())
        for info in targets:
            if info.uid not in exclude: self._queue_bytes(info.uid, frame_bytes, msg_type)

    def _queue_bytes(self, uid: int, frame_bytes: bytes, msg_type: int = 0) -> None:
        with self._clients_lock: info = self._clients.get(uid)
        if info is None: return

        if msg_type == MSG_SCREEN_FRAME:
            target_q, can_drop = info.media_queue, True
        elif msg_type in (MSG_FILE_META, MSG_FILE_CHUNK, MSG_FILE_TASK_META,
                          MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK):
            target_q, can_drop = info.file_queue, False
        else:
            target_q, can_drop = info.priority_queue, True

        try:
            if can_drop and target_q.full():
                try: target_q.get_nowait()
                except queue.Empty: pass
            target_q.put_nowait(frame_bytes)
        except queue.Full:
            if not can_drop:
                # 【致命Bug修复】：文件队列满时，绝不阻塞接收线程！直接断开该客户端连接。
                log.warn(TAG, f"File queue full for client {uid}, closing connection to prevent deadlock.")
                try: info.sock.close()
                except OSError: pass
                return
            
    def _remove_client(self, uid: int) -> None:
        with self._clients_lock: info = self._clients.pop(uid, None)
        if info is None: return

        nickname = info.nickname or f"房客{uid}"
        log.log(TAG, f"Removing client {uid} ({nickname})")

        try: info.sock.close()
        except OSError: pass
        if info.voice_sock:
            try: info.voice_sock.close()
            except OSError: pass

        try: info.priority_queue.put_nowait(False)
        except queue.Full: pass
        try: info.voice_send_queue.put_nowait(None)
        except queue.Full: pass

        leave_data = bytes([CMD_LEAVE]) + nickname.encode("utf-8")
        leave_frame = build_frame(MSG_COMMAND, uid, BROADCAST_ID, leave_data)
        self.broadcast(leave_frame)

        if self._on_client_left: self._on_client_left(uid, nickname)
