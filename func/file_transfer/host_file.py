# -*- coding: utf-8 -*-
"""
文件传输 —— 主机端处理器
职责：
  1. 接收房客发来的文件/文件夹（保存本地，保留目录结构）
  2. 中转房客之间的文件传输
  3. 主机主动发送文件或文件夹给指定房客
"""

import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, Signal

from common import logger as log
from core.server import Server
from core.protocol import (
    build_frame, MSG_FILE_META, MSG_FILE_CHUNK,
    HOST_ID, BROADCAST_ID
)
import config

TAG = "HostFile"


@dataclass
class IncomingTransfer:
    filename: str
    file_size: int
    sender_id: int
    target_id: int  # HOST_ID 表示发给主机
    received: int = 0
    file_path: str = ""
    base_name: str = ""  # 文件夹名（空=单文件）


class HostFileHandler(QObject):
    """
    主机文件处理器。
    信号：
      - progress(int, str, str): (percent, speed_str, eta_str)
      - file_complete(str): (saved_path)
      - status_changed(str): 状态文本
    """

    progress = Signal(int, str, str)
    file_complete = Signal(str)
    status_changed = Signal(str)

    def __init__(self, server: Server, save_dir: str = ""):
        super().__init__()
        self._server = server
        self._save_dir = save_dir or config.DOWNLOAD_DIR
        os.makedirs(self._save_dir, exist_ok=True)
        self._transfers: dict[int, IncomingTransfer] = {}  # sender_id -> transfer
        self._transfers_lock = threading.Lock()  # 保护并发访问

        # 注册消息处理器
        server.register_handler(MSG_FILE_META, self._handle_meta)
        server.register_handler(MSG_FILE_CHUNK, self._handle_chunk)

    # ═════════════════════════════════════════
    # 接收文件元数据
    # ═════════════════════════════════════════

    def _handle_meta(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        """处理文件元数据帧。"""
        meta = self._parse_meta(payload)
        if meta is None:
            return

        base_name, filename, file_size, actual_target = meta
        log.log(TAG, f"File meta from {sender_id}: [{base_name}] {filename} ({file_size} bytes) -> {actual_target}")

        if actual_target == HOST_ID:
            # 发给主机自己 → 准备接收
            if base_name:
                # 文件夹：save_dir/base_name/relative_path
                full_dir = os.path.join(self._save_dir, base_name, os.path.dirname(filename))
                os.makedirs(full_dir, exist_ok=True)
                part_path = os.path.join(self._save_dir, base_name, filename + ".part")
            else:
                part_path = os.path.join(self._save_dir, filename + ".part")

            transfer = IncomingTransfer(
                filename=filename, file_size=file_size,
                sender_id=sender_id, target_id=HOST_ID,
                file_path=part_path, base_name=base_name
            )
            with self._transfers_lock:
                self._transfers[sender_id] = transfer
            display = f"{base_name}/{filename}" if base_name else filename
            self.status_changed.emit(f"正在接收: {display} (来自用户{sender_id})")
        else:
            # 发给其他房客 → 中转：转发元数据
            relay_frame = build_frame(MSG_FILE_META, sender_id, actual_target, payload)
            self._server.send_to(actual_target, relay_frame)
            log.log(TAG, f"Relaying file meta to {actual_target}")

    # ═════════════════════════════════════════
    # 接收文件数据块
    # ═════════════════════════════════════════

    def _handle_chunk(self, msg_type: int, sender_id: int, target_id: int, payload: bytes) -> None:
        """处理文件数据块帧。"""
        with self._transfers_lock:
            transfer = self._transfers.get(sender_id)
        if transfer is None:
            # 可能是中转
            if target_id != HOST_ID and target_id != BROADCAST_ID:
                relay_frame = build_frame(MSG_FILE_CHUNK, sender_id, target_id, payload)
                self._server.send_to(target_id, relay_frame)
            return

        # 确保父目录存在
        os.makedirs(os.path.dirname(transfer.file_path), exist_ok=True)

        # 写入本地文件
        try:
            with open(transfer.file_path, "ab" if transfer.received > 0 else "wb") as f:
                f.write(payload)
            transfer.received += len(payload)

            # 进度更新（每 10 个块或接近完成时）
            if transfer.file_size > 0:
                percent = min(100, int(transfer.received * 100 / transfer.file_size))
                if percent >= 100 or transfer.received % (config.FILE_CHUNK_SIZE * 10) < len(payload):
                    self.progress.emit(percent, "", "")

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
                with self._transfers_lock:
                    self._transfers.pop(sender_id, None)

        except OSError as e:
            log.error(TAG, f"File write error: {e}")
            self.status_changed.emit(f"写入失败: {e}")
            with self._transfers_lock:
                self._transfers.pop(sender_id, None)

    # ═════════════════════════════════════════
    # 主机发送文件
    # ═════════════════════════════════════════

    def send_file(self, file_path: str, target_id: int) -> None:
        """主机发送单个文件给指定房客。"""
        threading.Thread(
            target=self._send_file_loop,
            args=(file_path, target_id),
            daemon=True, name=f"HostFileSend-{target_id}"
        ).start()

    def send_folder(self, folder_path: str, target_id: int) -> None:
        """主机发送文件夹给指定房客（保留目录结构）。"""
        threading.Thread(
            target=self._send_folder_loop,
            args=(folder_path, target_id),
            daemon=True, name=f"HostFolderSend-{target_id}"
        ).start()

    def _send_file_loop(self, file_path: str, target_id: int, base_name: str = "") -> None:
        """发送单个文件（可带 base_name 表示属于某文件夹）。"""
        try:
            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            name_bytes = filename.encode("utf-8")
            base_bytes = base_name.encode("utf-8")

            # 构建元数据: [4B base_name_len][base_name][4B name_len][name][8B size][4B target_id]
            meta = bytearray()
            meta.extend(struct.pack("!I", len(base_bytes)))
            meta.extend(base_bytes)
            meta.extend(struct.pack("!I", len(name_bytes)))
            meta.extend(name_bytes)
            meta.extend(struct.pack("!Q", file_size))
            meta.extend(struct.pack("!I", target_id))

            meta_frame = build_frame(MSG_FILE_META, HOST_ID, target_id, bytes(meta))
            self._server.send_to(target_id, meta_frame)

            display = f"{base_name}/{filename}" if base_name else filename
            log.log(TAG, f"Sending {display} ({file_size} bytes) to {target_id}")
            self.status_changed.emit(f"正在发送: {display} → 用户{target_id}")

            # 发送数据块
            sent = 0
            start_time = time.time()
            last_report = start_time
            last_bytes = 0

            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(config.FILE_CHUNK_SIZE)
                    if not chunk:
                        break
                    chunk_frame = build_frame(MSG_FILE_CHUNK, HOST_ID, target_id, chunk)
                    self._server.send_to(target_id, chunk_frame)
                    sent += len(chunk)

                    # 限速
                    time.sleep(config.FILE_SEND_DELAY)

                    # 进度更新
                    now = time.time()
                    if now - last_report >= 1.0:
                        elapsed = now - last_report
                        speed = (sent - last_bytes) / elapsed
                        percent = min(100, int(sent * 100 / file_size)) if file_size > 0 else 100
                        speed_str = self._format_speed(speed)
                        remaining = file_size - sent
                        eta = remaining / speed if speed > 0 else 0
                        eta_str = self._format_eta(eta)
                        self.progress.emit(percent, speed_str, eta_str)
                        last_report = now
                        last_bytes = sent

            self.progress.emit(100, "完成", "")

        except OSError as e:
            log.error(TAG, f"File send error: {e}")
            self.status_changed.emit(f"发送失败: {e}")

    def _send_folder_loop(self, folder_path: str, target_id: int) -> None:
        """遍历文件夹并逐个发送文件。"""
        folder_name = os.path.basename(folder_path)
        file_list = []
        for root, dirs, files in os.walk(folder_path):
            for fname in files:
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, folder_path)
                file_list.append((full_path, rel_path))

        total = len(file_list)
        self.status_changed.emit(f"正在发送文件夹: {folder_name} ({total} 个文件)")
        log.log(TAG, f"Sending folder '{folder_name}' with {total} files to {target_id}")

        for idx, (full_path, rel_path) in enumerate(file_list, 1):
            try:
                file_size = os.path.getsize(full_path)
                rel_bytes = rel_path.encode("utf-8")
                base_bytes = folder_name.encode("utf-8")

                # 构建元数据
                meta = bytearray()
                meta.extend(struct.pack("!I", len(base_bytes)))
                meta.extend(base_bytes)
                meta.extend(struct.pack("!I", len(rel_bytes)))
                meta.extend(rel_bytes)
                meta.extend(struct.pack("!Q", file_size))
                meta.extend(struct.pack("!I", target_id))

                meta_frame = build_frame(MSG_FILE_META, HOST_ID, target_id, bytes(meta))
                self._server.send_to(target_id, meta_frame)

                self.status_changed.emit(f"[{idx}/{total}] {rel_path}")

                # 发送数据块
                with open(full_path, "rb") as f:
                    while True:
                        chunk = f.read(config.FILE_CHUNK_SIZE)
                        if not chunk:
                            break
                        chunk_frame = build_frame(MSG_FILE_CHUNK, HOST_ID, target_id, chunk)
                        self._server.send_to(target_id, chunk_frame)
                        time.sleep(config.FILE_SEND_DELAY)

                # 文件间短暂间隔
                time.sleep(0.01)

            except OSError as e:
                log.error(TAG, f"File send error ({rel_path}): {e}")
                continue

        self.status_changed.emit(f"文件夹发送完成: {folder_name} ({total} 个文件)")
        self.progress.emit(100, "完成", "")
        log.log(TAG, f"Folder sent: {folder_name} to {target_id}")

    # ═════════════════════════════════════════
    # 工具方法
    # ═════════════════════════════════════════

    @staticmethod
    def _parse_meta(payload: bytes) -> Optional[tuple[str, str, int, int]]:
        """
        解析文件元数据。
        新格式: [4B base_name_len][base_name][4B name_len][name][8B size][4B target_id]
        返回: (base_name, filename, file_size, target_id)
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
        """清理未完成的 .part 文件。"""
        with self._transfers_lock:
            transfers_snapshot = list(self._transfers.values())
            self._transfers.clear()
        for transfer in transfers_snapshot:
            try:
                if os.path.exists(transfer.file_path):
                    os.remove(transfer.file_path)
            except OSError:
                pass
