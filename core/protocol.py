# -*- coding: utf-8 -*-
"""
统一通信协议
"""

import struct
import time
from typing import Optional

# ─────────────────────────────────────────────
# 消息类型 (Message Type)
# ─────────────────────────────────────────────
MSG_TEXT: int = 0x01           
MSG_FILE_META: int = 0x02      # 废弃，保留兼容
MSG_FILE_CHUNK: int = 0x03     
MSG_SCREEN_FRAME: int = 0x04   
MSG_COMMAND: int = 0x05        
MSG_VOICE: int = 0x06          
MSG_FILE_RESUME_REQ: int = 0x07  # 接收方请求续传 (Payload: JSON)
MSG_FILE_RESUME_ACK: int = 0x08  # 发送方确认续传 (Payload: 1B status)
MSG_FILE_TASK_META: int = 0x09   # 文件夹/断点续传任务元数据 (Payload: JSON)

# ─────────────────────────────────────────────
# 控制命令子类型
# ─────────────────────────────────────────────
CMD_JOIN: int = 0x01           
CMD_JOIN_ACK: int = 0x02       
CMD_LEAVE: int = 0x03          
CMD_USER_LIST: int = 0x04      
CMD_SCREEN_START: int = 0x10   
CMD_SCREEN_STOP: int = 0x11    
CMD_CHAT_BROADCAST: int = 0x20 

# ─────────────────────────────────────────────
# ID 约定
# ─────────────────────────────────────────────
HOST_ID: int = 0               
BROADCAST_ID: int = 0xFFFF     

TCP_HEADER_SIZE: int = 13
MAX_FRAME_SIZE: int = 16 * 1024 * 1024

def generate_transfer_id() -> int:
    return int(time.time_ns() & 0xFFFFFFFF)

def build_frame(msg_type: int, sender_id: int, target_id: int, data: bytes = b"") -> bytes:
    body = struct.pack("!BII", msg_type, sender_id, target_id) + data
    length = len(body)
    return struct.pack("!I", length) + body

def parse_frame_header(data: bytes) -> tuple[int, int, int]:
    msg_type, sender_id, target_id = struct.unpack("!BII", data[:9])
    return msg_type, sender_id, target_id

def read_frame(recv_exact) -> Optional[tuple[int, int, int, bytes]]:
    try:
        length_bytes = recv_exact(4)
        length = struct.unpack("!I", length_bytes)[0]
        if length > MAX_FRAME_SIZE:
            raise ConnectionError(f"Frame too large: {length} bytes")
        body = recv_exact(length)
        msg_type, sender_id, target_id = parse_frame_header(body)
        payload = body[9:]
        return msg_type, sender_id, target_id, payload
    except (ConnectionError, OSError, struct.error):
        return None

class NicknameRegistry:
    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._map: dict[int, str] = {}

    def set(self, uid: int, nickname: str) -> None:
        with self._lock: self._map[uid] = nickname

    def get(self, uid: int) -> str:
        with self._lock: return self._map.get(uid, f"用户{uid}")

    def remove(self, uid: int) -> None:
        with self._lock: self._map.pop(uid, None)

    def get_all(self) -> dict[int, str]:
        with self._lock: return dict(self._map)

    def clear(self) -> None:
        with self._lock: self._map.clear()
