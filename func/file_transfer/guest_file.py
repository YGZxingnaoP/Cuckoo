# -*- coding: utf-8 -*-
"""
文件传输 —— 房客端接收器
职责：
  接收房主转发来的文件（保存本地，支持文件夹结构）
  内置异步写入线程，避免磁盘 IO 阻塞 UI 主线程。
"""

import os
import queue
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, Signal

from common import logger as log
from core.protocol import MSG_FILE_META, MSG_FILE_CHUNK
import config

TAG = "GuestFileRecv"


@dataclass
class IncomingTransfer:
    filename: str
    file_size: int
    base_name: str  # 文件夹名（空=单文件）
    received: int = 0
    file_path: str = ""


@dataclass
class _WriteTask:
    sender_id: int
    payload: bytes
    file_path: str
    is_first_chunk: bool


class GuestFileReceiver(QObject):
    """
    房客文件接收器。
    信号：
      - progress(int, str, str): (percent, speed_str, eta_str)
      - file_complete(str): (saved_path)
      - status_changed(str): 状态文本
    """

    progress = Signal(int, str, str)
    file_complete = Signal(str)
    status_changed = Signal(str)

    def __init__(self, save_dir: str = ""):
        super().__init__()
        self._save_dir = save_dir or config.DOWNLOAD_DIR
        os.makedirs(self._save_dir, exist_ok=True)
        self._transfers: dict[int, IncomingTransfer] = {}  # sender_id -> transfer
        self._files_opened: set[int] = set()  # 已写入首块的 sender_id（避免跨线程竞争）
        self._start_times: dict[int, float] = {}
        self._last_reports: dict[int, float] = {}
        self._last_bytes_map: dict[int, int] = {}

        # 异步写入队列与线程
        self._write_queue: queue.Queue = queue.Queue()
        self._write_thread: Optional[threading.Thread] = None
        self._write_running = False
        self._start_write_thread()

    # ═════════════════════════════════════════
    # 异步写入线程
    # ═════════════════════════════════════════

    def _start_write_thread(self) -> None:
        """启动后台文件写入线程。"""
        self._write_running = True
        self._write_thread = threading.Thread(
            target=self._write_loop, daemon=True, name="GuestFileWriter"
        )
        self._write_thread.start()
        log.log(TAG, "Write thread started")

    def _write_loop(self) -> None:
        """写入线程：从队列取任务，执行磁盘写入。"""
        while self._write_running:
            try:
                task = self._write_queue.get(timeout=0.5)
                if task is None:  # 哨兵值
                    break
                self._do_write(task)
            except queue.Empty:
                continue
            except Exception as e:
                log.error(TAG, f"Write loop error: {e}")
        log.log(TAG, "Write thread stopped")

    def _do_write(self, task: '_WriteTask') -> None:
        """执行单个写入任务并处理进度/完成逻辑。"""
        transfer = self._transfers.get(task.sender_id)
        if transfer is None:
            return

        try:
            os.makedirs(os.path.dirname(transfer.file_path), exist_ok=True)
            mode = "wb" if task.is_first_chunk else "ab"
            with open(transfer.file_path, mode) as f:
                f.write(task.payload)
            transfer.received += len(task.payload)

            # 进度更新
            if transfer.file_size > 0:
                percent = min(100, int(transfer.received * 100 / transfer.file_size))
                now = time.time()
                last_report = self._last_reports.get(task.sender_id, now)
                if now - last_report >= 1.0 or percent >= 100:
                    elapsed = now - last_report
                    last_b = self._last_bytes_map.get(task.sender_id, 0)
                    speed = (transfer.received - last_b) / elapsed if elapsed > 0 else 0
                    speed_str = self._format_speed(speed)
                    remaining = transfer.file_size - transfer.received
                    eta = remaining / speed if speed > 0 else 0
                    eta_str = self._format_eta(eta)
                    self.progress.emit(percent, speed_str, eta_str)
                    self._last_reports[task.sender_id] = now
                    self._last_bytes_map[task.sender_id] = transfer.received

            # 完成检测
            if transfer.received >= transfer.file_size:
                if transfer.base_name:
                    final_path = os.path.join(self._save_dir, transfer.base_name, transfer.filename)
                else:
                    final_path = os.path.join(self._save_dir, transfer.filename)
                final_path = self._unique_path(final_path)
                os.rename(transfer.file_path, final_path)
                self.file_complete.emit(final_path)
                display = f"{transfer.base_name}/{transfer.filename}" if transfer.base_name else transfer.filename
                self.status_changed.emit(f"接收完成: {display}")
                log.log(TAG, f"File saved: {final_path}")
                del self._transfers[task.sender_id]
                self._files_opened.discard(task.sender_id)
                self._start_times.pop(task.sender_id, None)
                self._last_reports.pop(task.sender_id, None)
                self._last_bytes_map.pop(task.sender_id, None)

        except OSError as e:
            log.error(TAG, f"File write error: {e}")
            self.status_changed.emit(f"写入失败: {e}")
            self._transfers.pop(task.sender_id, None)
            self._files_opened.discard(task.sender_id)
            self._start_times.pop(task.sender_id, None)
            self._last_reports.pop(task.sender_id, None)
            self._last_bytes_map.pop(task.sender_id, None)

    def handle_file_meta(self, sender_id: int, payload: bytes) -> None:
        """处理文件元数据帧。"""
        meta = self._parse_meta(payload)
        if meta is None:
            return

        base_name, filename, file_size, _target_id = meta
        log.log(TAG, f"File meta from {sender_id}: [{base_name}] {filename} ({file_size} bytes)")

        if base_name:
            # 文件夹：save_dir/base_name/relative_path
            full_dir = os.path.join(self._save_dir, base_name, os.path.dirname(filename))
            os.makedirs(full_dir, exist_ok=True)
            part_path = os.path.join(self._save_dir, base_name, filename + ".part")
        else:
            part_path = os.path.join(self._save_dir, filename + ".part")

        self._transfers[sender_id] = IncomingTransfer(
            filename=filename, file_size=file_size,
            base_name=base_name, file_path=part_path
        )
        now = time.time()
        self._start_times[sender_id] = now
        self._last_reports[sender_id] = now
        self._last_bytes_map[sender_id] = 0

        display = f"{base_name}/{filename}" if base_name else filename
        self.status_changed.emit(f"正在接收: {display}")

    def handle_file_chunk(self, sender_id: int, payload: bytes) -> None:
        """处理文件数据块帧（仅入队，由后台线程落盘，不阻塞 UI）。"""
        transfer = self._transfers.get(sender_id)
        if transfer is None:
            return

        # 用 _files_opened 集合判断是否首块，避免读取被写入线程修改的 received 字段
        is_first = sender_id not in self._files_opened
        self._files_opened.add(sender_id)
        task = _WriteTask(
            sender_id=sender_id,
            payload=payload,
            file_path=transfer.file_path,
            is_first_chunk=is_first
        )
        self._write_queue.put(task)

    @staticmethod
    def _parse_meta(payload: bytes) -> Optional[tuple[str, str, int, int]]:
        """
        解析文件元数据。
        格式: [4B base_name_len][base_name][4B name_len][name][8B size][4B target_id]
        """
        try:
            offset = 0
            base_name_len = struct.unpack("!I", payload[offset:offset + 4])[0]
            offset += 4
            base_name = payload[offset:offset + base_name_len].decode("utf-8")
            offset += base_name_len
            name_len = struct.unpack("!I", payload[offset:offset + 4])[0]
            offset += 4
            filename = payload[offset:offset + name_len].decode("utf-8")
            offset += name_len
            file_size = struct.unpack("!Q", payload[offset:offset + 8])[0]
            offset += 8
            target_id = struct.unpack("!I", payload[offset:offset + 4])[0]
            return base_name, filename, file_size, target_id
        except (struct.error, UnicodeDecodeError, IndexError):
            log.error(TAG, "Failed to parse file meta")
            return None

    @staticmethod
    def _unique_path(path: str) -> str:
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        counter = 1
        while True:
            new_path = f"{base}({counter}){ext}"
            if not os.path.exists(new_path):
                return new_path
            counter += 1

    @staticmethod
    def _format_speed(bps: float) -> str:
        if bps >= 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} MB/s"
        elif bps >= 1024:
            return f"{bps / 1024:.1f} KB/s"
        else:
            return f"{bps:.0f} B/s"

    @staticmethod
    def _format_eta(seconds: float) -> str:
        if seconds <= 0:
            return "即将完成"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h > 0:
            return f"{h}h{m}m{s}s"
        elif m > 0:
            return f"{m}m{s}s"
        else:
            return f"{s}s"

    def cleanup(self) -> None:
        """清理未完成的 .part 文件并停止写入线程。"""
        # 停止写入线程
        self._write_running = False
        self._write_queue.put(None)
        if self._write_thread:
            self._write_thread.join(timeout=3)
        for transfer in self._transfers.values():
            try:
                if os.path.exists(transfer.file_path):
                    os.remove(transfer.file_path)
            except OSError:
                pass
        self._transfers.clear()
        self._files_opened.clear()
        self._start_times.clear()
        self._last_reports.clear()
        self._last_bytes_map.clear()
