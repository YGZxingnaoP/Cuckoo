# -*- coding: utf-8 -*-
"""
文件传输公共基础设施
───────────────────────────────────────────
包含：工具函数、_BitSet 位图、_TaskStore 持久化存储
"""

import os
import json
import base64
import hashlib
import random
import time
import struct
import threading
from typing import Optional, Callable

from common import logger as log
import config

TAG = "FileTransfer"

# ── 二进制协议包头 ──
CHUNK_HEADER = struct.Struct("!IIQ")   # task_id(4B) + file_index(4B) + chunk_seq(8B)
ACK_STRUCT   = struct.Struct("!IIq")   # task_id(4B) + file_index(4B) + acked_seq(8B)
CANCEL_FMT   = struct.Struct("!I")     # task_id(4B)
EOF_SEQ      = 0xFFFFFFFFFFFFFFFF       # 文件结束标记


# ══════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════

def generate_task_id() -> int:
    """生成32位唯一任务ID"""
    ts = int(time.time_ns()) & 0xFFFFFFFF
    rnd = random.getrandbits(16)
    return ((ts >> 16) << 16) | (rnd & 0xFFFF)


def fmt_size(n: int) -> str:
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def fmt_speed(b: float) -> str:
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB/s"
    if b >= 1024:
        return f"{b / 1024:.1f} KB/s"
    return f"{b:.0f} B/s"


def sanitize_rel_path(rel: str) -> str:
    """清洗相对路径，拒绝路径遍历攻击"""
    if os.path.isabs(rel):
        raise ValueError(f"Absolute path rejected: {rel}")
    normalized = os.path.normpath(rel)
    if normalized.startswith("..") or os.path.isabs(normalized):
        raise ValueError(f"Path traversal detected: {rel} -> {normalized}")
    return normalized.replace("\\", "/")


def is_large_file(fi: dict) -> bool:
    """判断是否为大文件（≥2GB，启用位图随机写 + chunk 重传）"""
    return fi.get("size", 0) >= config.LARGE_FILE_THRESHOLD


def calc_file_md5(path: str) -> str:
    """流式计算文件 MD5（避免整文件读入内存）"""
    md5 = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(config.FILE_CHUNK_SIZE)
                if not chunk:
                    break
                md5.update(chunk)
    except OSError as e:
        log.error(TAG, f"MD5 calc failed for {path}: {e}")
        return ""
    return md5.hexdigest()


# ══════════════════════════════════════════════════
# 位图集合
# ══════════════════════════════════════════════════

class _BitSet:
    """
    位图集合：用于大文件记录已接收的 chunk seq。
    相比 Python set，内存占用降低约 300 倍（8万 chunk 仅 ~10KB）。
    提供 O(1) 的 add / contains / count，及序列化（base64）用于断点续传。
    """

    __slots__ = ("_bits", "_count", "_total")

    def __init__(self, total: int):
        self._total = max(0, total)
        self._bits = bytearray((self._total + 7) // 8)
        self._count = 0

    def add(self, seq: int) -> bool:
        """添加一个 seq，返回 True 表示新增，False 表示已存在"""
        if seq < 0 or seq >= self._total:
            return False
        byte_idx = seq >> 3
        bit = 1 << (seq & 7)
        if self._bits[byte_idx] & bit:
            return False
        self._bits[byte_idx] |= bit
        self._count += 1
        return True

    def contains(self, seq: int) -> bool:
        if seq < 0 or seq >= self._total:
            return False
        return bool(self._bits[seq >> 3] & (1 << (seq & 7)))

    def count(self) -> int:
        return self._count

    def is_complete(self) -> bool:
        return self._count >= self._total

    def missing(self) -> list:
        """返回所有缺失的 seq（仅在 EOF 时调用，全量遍历可接受）"""
        return [i for i in range(self._total) if not self.contains(i)]

    def contiguous_prefix(self) -> int:
        """返回从 0 开始的连续已接收前缀长度（seq 数量）。
        用于大文件 ACK：只确认连续前缀，缺口后不虚高。"""
        prefix = 0
        for byte in self._bits:
            if byte == 0xFF:
                prefix += 8
            else:
                # 该字节内有 0 位，逐位检查
                for bit in range(8):
                    if byte & (1 << bit):
                        prefix += 1
                    else:
                        return min(prefix, self._total)
                return min(prefix, self._total)
        return min(prefix, self._total)

    def clear(self) -> None:
        self._bits = bytearray(len(self._bits))
        self._count = 0

    def to_bytes(self) -> bytes:
        return bytes(self._bits)

    @classmethod
    def from_bytes(cls, total: int, data: bytes) -> "_BitSet":
        bs = cls(total)
        if data:
            bs._bits = bytearray(data[:len(bs._bits)])
            bs._count = sum(bin(b).count("1") for b in bs._bits)
        return bs

    def to_base64(self) -> str:
        return base64.b64encode(self.to_bytes()).decode("ascii")

    @classmethod
    def from_base64(cls, total: int, b64: str) -> "_BitSet":
        try:
            data = base64.b64decode(b64)
            return cls.from_bytes(total, data)
        except Exception:
            return cls(total)


# ══════════════════════════════════════════════════
# 持久化存储
# ══════════════════════════════════════════════════

class _TaskStore:
    """任务状态的 JSON 文件存储（线程安全）"""

    def __init__(self, base_dir: str):
        self._recv_dir = os.path.join(base_dir, ".resume_tasks")
        self._send_dir = os.path.join(base_dir, ".send_tasks")
        os.makedirs(self._recv_dir, exist_ok=True)
        os.makedirs(self._send_dir, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def _clean(obj):
        """递归清理不可 JSON 序列化的对象（set→list、_BitSet→base64 等）"""
        if isinstance(obj, _BitSet):
            return obj.to_base64()
        if isinstance(obj, dict):
            return {k: _TaskStore._clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_TaskStore._clean(v) for v in obj]
        if isinstance(obj, set):
            return sorted(obj)
        return obj

    @staticmethod
    def _restore_bitset(fi: dict) -> None:
        """恢复大文件条目的 received_seqs（base64/list → _BitSet）"""
        cc = fi.get("chunk_count", 0)
        rs = fi.get("received_seqs")
        if isinstance(rs, str):
            fi["received_seqs"] = _BitSet.from_base64(cc, rs)
        elif isinstance(rs, list):
            bs = _BitSet(cc)
            for seq in rs:
                bs.add(seq)
            fi["received_seqs"] = bs
        elif not isinstance(rs, _BitSet):
            fi["received_seqs"] = _BitSet(cc)

    def save_recv(self, task: dict) -> None:
        tid = task["task_id"]
        path = os.path.join(self._recv_dir, f"{tid}.json")
        with self._lock:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self._clean(task), f)
            except Exception as e:
                log.warn(TAG, f"Save recv task {tid} failed: {e}")

    def delete_recv(self, tid: int) -> None:
        path = os.path.join(self._recv_dir, f"{tid}.json")
        with self._lock:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                log.warn(TAG, f"Delete recv task {tid} failed: {e}")

    def load_recv_tasks(self) -> list[dict]:
        tasks = []
        try:
            for fn in os.listdir(self._recv_dir):
                if fn.endswith(".json"):
                    try:
                        with open(os.path.join(self._recv_dir, fn), encoding="utf-8") as f:
                            t = json.load(f)
                            for fi in t.get("files", []):
                                self._restore_bitset(fi)
                            tasks.append(t)
                    except Exception as e:
                        log.warn(TAG, f"Load recv task {fn} failed: {e}")
        except FileNotFoundError:
            pass
        return tasks

    def save_send(self, task: dict) -> None:
        tid = task["task_id"]
        path = os.path.join(self._send_dir, f"{tid}.json")
        with self._lock:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self._clean(task), f)
            except Exception as e:
                log.warn(TAG, f"Save send task {tid} failed: {e}")

    def delete_send(self, tid: int) -> None:
        path = os.path.join(self._send_dir, f"{tid}.json")
        with self._lock:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                log.warn(TAG, f"Delete send task {tid} failed: {e}")

    def load_send_tasks(self) -> list[dict]:
        """加载发送任务（断点续传：接收端重启后请求续传时恢复）"""
        tasks = []
        try:
            for fn in os.listdir(self._send_dir):
                if fn.endswith(".json"):
                    try:
                        with open(os.path.join(self._send_dir, fn), encoding="utf-8") as f:
                            t = json.load(f)
                            # 恢复大文件 received_seqs 位图，保证续传时能正确计算缺失 seq
                            for fi in t.get("files", []):
                                self._restore_bitset(fi)
                            tasks.append(t)
                    except Exception as e:
                        log.warn(TAG, f"Load send task {fn} failed: {e}")
        except FileNotFoundError:
            pass
        return tasks
