# -*- coding: utf-8 -*-
"""
统一通信协议
TCP 帧格式: [4字节总长度][1字节消息类型][4字节发送者ID][4字节目标ID][N字节数据体]
UDP 语音帧: [2字节发送者ID][2字节序列号][N字节PCM数据]
"""

import struct
from typing import Optional

# ─────────────────────────────────────────────
# 消息类型 (Message Type)
# ─────────────────────────────────────────────
MSG_TEXT: int = 0x01           # 文本消息
MSG_FILE_META: int = 0x02      # 文件元数据（文件名、大小、目标）
MSG_FILE_CHUNK: int = 0x03     # 文件数据块
MSG_SCREEN_FRAME: int = 0x04   # 投屏视频帧
MSG_COMMAND: int = 0x05        # 控制信令
MSG_VOICE: int = 0x06          # 语音数据（TCP 传输）

# ─────────────────────────────────────────────
# 控制命令子类型（存放在数据体首字节）
# ─────────────────────────────────────────────
CMD_JOIN: int = 0x01           # 房客请求加入
CMD_JOIN_ACK: int = 0x02       # 房主确认加入（携带分配的 ID）
CMD_LEAVE: int = 0x03          # 离开通知
CMD_USER_LIST: int = 0x04      # 在线用户列表
CMD_SCREEN_START: int = 0x10   # 开始投屏
CMD_SCREEN_STOP: int = 0x11    # 停止投屏
CMD_CHAT_BROADCAST: int = 0x20  # 文本广播

# ─────────────────────────────────────────────
# ID 约定
# ─────────────────────────────────────────────
HOST_ID: int = 0               # 房主 ID
BROADCAST_ID: int = 0xFFFF     # 广播（全体）

# ─────────────────────────────────────────────
# TCP 帧头: [4B total_length][1B type][4B sender_id][4B target_id] = 13 字节
# ─────────────────────────────────────────────
TCP_HEADER_SIZE: int = 13
_TCP_HEADER_FORMAT: str = "!IBII"  # 大端序

# 单帧最大允许大小（16 MB）—— 防止恶意客户端发送超大长度导致内存耗尽
MAX_FRAME_SIZE: int = 16 * 1024 * 1024


def build_frame(msg_type: int, sender_id: int, target_id: int, data: bytes = b"") -> bytes:
    """
    构建完整的 TCP 帧（可直接发送）。
    :return: 包含长度前缀的完整帧字节
    """
    body = struct.pack("!BII", msg_type, sender_id, target_id) + data
    length = len(body)
    return struct.pack("!I", length) + body


def parse_frame_header(data: bytes) -> tuple[int, int, int]:
    """
    解析 TCP 帧头（9字节：1B type + 4B sender + 4B target）。
    注意：调用前应已读取 4 字节长度前缀并据此读取 body。
    :param data: 帧体（不含长度前缀）
    :return: (msg_type, sender_id, target_id)
    """
    msg_type, sender_id, target_id = struct.unpack("!BII", data[:9])
    return msg_type, sender_id, target_id


def read_frame(recv_exact) -> Optional[tuple[int, int, int, bytes]]:
    """
    从 TCP 流中读取一帧。
    :param recv_exact: 精确接收 N 字节的 callable(n) -> bytes
    :return: (msg_type, sender_id, target_id, payload) 或 None
    """
    try:
        length_bytes = recv_exact(4)
        length = struct.unpack("!I", length_bytes)[0]
        # 安全检查：拒绝超大帧，防止内存耗尽攻击
        if length > MAX_FRAME_SIZE:
            raise ConnectionError(
                f"Frame too large: {length} bytes (max {MAX_FRAME_SIZE})"
            )
        body = recv_exact(length)
        msg_type, sender_id, target_id = parse_frame_header(body)
        payload = body[9:]
        return msg_type, sender_id, target_id, payload
    except (ConnectionError, OSError, struct.error):
        return None


# ─────────────────────────────────────────────
# UDP 语音帧
# ─────────────────────────────────────────────
VOICE_HEADER_SIZE: int = 4  # 2B sender_id + 2B seq
_VOICE_HEADER_FORMAT: str = "!HH"


def build_voice_packet(sender_id: int, seq: int, pcm_data: bytes) -> bytes:
    """构建 UDP 语音帧。"""
    header = struct.pack(_VOICE_HEADER_FORMAT, sender_id, seq)
    return header + pcm_data


def parse_voice_packet(data: bytes) -> Optional[tuple[int, int, bytes]]:
    """
    解析 UDP 语音帧。
    :return: (sender_id, seq, pcm_data) 或 None
    """
    if len(data) < VOICE_HEADER_SIZE:
        return None
    sender_id, seq = struct.unpack(_VOICE_HEADER_FORMAT, data[:VOICE_HEADER_SIZE])
    return sender_id, seq, data[VOICE_HEADER_SIZE:]


# ─────────────────────────────────────────────
# 昵称注册表（UI 辅助）
# ─────────────────────────────────────────────
class NicknameRegistry:
    """线程安全的 ID→昵称 映射表。"""

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._map: dict[int, str] = {}

    def set(self, uid: int, nickname: str) -> None:
        with self._lock:
            self._map[uid] = nickname

    def get(self, uid: int) -> str:
        with self._lock:
            return self._map.get(uid, f"用户{uid}")

    def remove(self, uid: int) -> None:
        with self._lock:
            self._map.pop(uid, None)

    def get_all(self) -> dict[int, str]:
        with self._lock:
            return dict(self._map)

    def clear(self) -> None:
        with self._lock:
            self._map.clear()
