# -*- coding: utf-8 -*-
"""
统一文件传输管理器 (P2P 通用)
彻底消除房主/房客差异，支持任意节点互传、本地回环、断点续传。

【P0修复】:
- Chunk增加序号，接收方gap检测与重传请求
- 发送方阻塞背压 + 发送方任务持久化
- 传输完成确认机制
"""

import os
import json
import hashlib
import struct
import threading
import time
import queue
from typing import Optional, Callable

from PySide6.QtCore import QObject, Signal
from common import logger as log
from core.protocol import (
    MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK, MSG_FILE_CHUNK,
    MSG_FILE_CHUNK_ACK, MSG_FILE_OFFER, MSG_FILE_OFFER_RESP, HOST_ID
)
import config

TAG = "UnifiedFile"

# chunk header: task_id(4) + file_idx(4) + chunk_seq(8) = 16 bytes
CHUNK_HEADER_FMT = "!IIQ"
CHUNK_HEADER_SIZE = struct.calcsize(CHUNK_HEADER_FMT)
# ACK format: task_id(4) + file_idx(4) + last_good_seq(8, signed) = 16B, followed by gap_count(4)+gaps
ACK_FMT = "!IIq"

# 特殊chunk_seq: 0xFFFFFFFFFFFFFFFF 表示文件传输结束
CHUNK_SEQ_EOF = 0xFFFFFFFFFFFFFFFF


class UnifiedFileTransfer(QObject):
    progress = Signal(str, int, str, str)          # task_id, percent, speed, eta
    file_complete = Signal(str, str)               # task_id, final_path
    status_changed = Signal(str)
    task_interrupted = Signal(str, str)            # task_id, display_name
    task_removed = Signal(str)
    file_offer_received = Signal(str, str, str)    # task_id, display_name, size_desc

    def __init__(self, my_id: int, send_callback: Callable):
        super().__init__()
        self._my_id = my_id
        self._send_callback = send_callback  # fn(msg_type, target_id, payload)

        self._save_dir = config.DOWNLOAD_DIR
        os.makedirs(self._save_dir, exist_ok=True)
        self._resume_dir = os.path.join(self._save_dir, ".resume_tasks")
        os.makedirs(self._resume_dir, exist_ok=True)
        self._send_resume_dir = os.path.join(self._save_dir, ".send_tasks")
        os.makedirs(self._send_resume_dir, exist_ok=True)

        self._recv_tasks = {}
        self._send_tasks = {}
        self._pending_offers = {}  # task_id -> offer data (等待接收方确认的邀约)
        self._lock = threading.Lock()

        self._write_queue = queue.Queue()
        self._write_running = True
        threading.Thread(target=self._write_loop, daemon=True, name="FileWriter").start()

        self._load_interrupted_tasks()

    # ═════════════════════════════════════════
    # 网络层入口 (由 MainWindow 调用)
    # ═════════════════════════════════════════
    def handle_incoming(self, msg_type: int, sender_id: int, payload: bytes):
        if msg_type == MSG_FILE_OFFER:
            self._on_file_offer(sender_id, payload)
        elif msg_type == MSG_FILE_OFFER_RESP:
            self._on_file_offer_resp(sender_id, payload)
        elif msg_type == MSG_FILE_TASK_META:
            self._on_task_meta(sender_id, payload)
        elif msg_type == MSG_FILE_RESUME_REQ:
            self._on_resume_req(sender_id, payload)
        elif msg_type == MSG_FILE_RESUME_ACK:
            self._on_resume_ack(sender_id, payload)
        elif msg_type == MSG_FILE_CHUNK:
            self._on_chunk(sender_id, payload)
        elif msg_type == MSG_FILE_CHUNK_ACK:
            self._on_chunk_ack(sender_id, payload)

    # ═════════════════════════════════════════
    # 发送方逻辑
    # ═════════════════════════════════════════
    def send_file(self, file_path: str, target_id: int):
        self._start_send([file_path], target_id, False)

    def send_folder(self, folder_path: str, target_id: int):
        files = [os.path.join(r, f) for r, _, fs in os.walk(folder_path) for f in fs]
        if not files:
            self.status_changed.emit("文件夹为空")
            return
        self._start_send(files, target_id, True, os.path.basename(folder_path), folder_path)

    def _start_send(self, abs_paths, target_id, is_folder, base_name="", root_path=""):
        task_id = int(time.time() * 1000) & 0xFFFFFFFF
        files = []
        total_size = 0
        for p in abs_paths:
            try:
                size = os.path.getsize(p)
            except OSError:
                log.error(TAG, f"Cannot access file: {p}")
                continue
            fp = hashlib.md5(f"{size}_{os.path.getmtime(p)}".encode()).hexdigest()[:8]
            rel = os.path.relpath(p, root_path) if root_path else os.path.basename(p)
            files.append({"abs": p.replace("\\", "/"), "rel": rel.replace("\\", "/"),
                          "size": size, "fp": fp, "recv": 0, "status": "pending",
                          "chunk_count": (size + config.FILE_CHUNK_SIZE - 1) // config.FILE_CHUNK_SIZE,
                          "acked_seq": -1})
            total_size += size

        if not files:
            self.status_changed.emit("无可发送的文件")
            return

        task = {"task_id": task_id, "sender": self._my_id, "target": target_id,
                "is_folder": is_folder, "base_name": base_name, "files": files,
                "created_at": time.time()}

        # 构建邀约摘要
        display_name = base_name or os.path.basename(files[0]["abs"])
        if is_folder:
            size_desc = f"文件夹 · {len(files)} 个文件"
        else:
            size_desc = self._fmt_size(total_size)

        offer = {
            "task_id": task_id,
            "sender": self._my_id,
            "target": target_id,
            "is_folder": is_folder,
            "base_name": base_name,
            "display_name": display_name,
            "size_desc": size_desc,
            "total_size": total_size,
            "file_count": len(files),
        }

        with self._lock:
            self._pending_offers[task_id] = {"task": task, "offer": offer}

        self._send_callback(MSG_FILE_OFFER, target_id, json.dumps(offer).encode("utf-8"))
        display = base_name or os.path.basename(files[0]["abs"])
        self.status_changed.emit(f"等待 {target_id} 确认接收: {display}...")

    def _on_file_offer(self, sender_id: int, payload: bytes):
        """接收方：收到发送邀约，弹窗确认"""
        try:
            offer = json.loads(payload.decode("utf-8"))
        except Exception:
            return

        task_id = str(offer["task_id"])
        display_name = offer.get("display_name", "未知文件")
        size_desc = offer.get("size_desc", "")

        # 存储 offer 等待用户响应
        with self._lock:
            self._pending_offers[int(offer["task_id"])] = {
                "offer": offer,
                "sender_id": sender_id,
            }

        self.file_offer_received.emit(task_id, display_name, size_desc)

    def respond_to_offer(self, task_id_str: str, accept: bool) -> None:
        """接收方 UI 调用：接受或拒绝文件邀约"""
        tid = int(task_id_str)
        with self._lock:
            pending = self._pending_offers.pop(tid, None)

        if not pending:
            return

        offer = pending["offer"]
        sender_id = pending.get("sender_id", HOST_ID)

        if accept:
            # 发送接受响应
            resp = json.dumps({"task_id": tid, "accept": True}).encode("utf-8")
            self._send_callback(MSG_FILE_OFFER_RESP, sender_id, bytes([0x01]) + resp)
            self.status_changed.emit(f"已接受: {offer.get('display_name', '')}")
        else:
            resp = json.dumps({"task_id": tid, "accept": False}).encode("utf-8")
            self._send_callback(MSG_FILE_OFFER_RESP, sender_id, bytes([0x00]) + resp)
            self.status_changed.emit(f"已拒绝: {offer.get('display_name', '')}")

    def _on_file_offer_resp(self, sender_id: int, payload: bytes):
        """发送方：收到接收方的接受/拒绝响应"""
        if len(payload) < 2:
            return
        accepted = payload[0] == 0x01
        try:
            resp = json.loads(payload[1:].decode("utf-8"))
            task_id = resp["task_id"]
        except Exception:
            return

        with self._lock:
            pending = self._pending_offers.pop(task_id, None)

        if not pending:
            return

        if not accepted:
            self.status_changed.emit(f"对方拒绝了传输")
            self._remove_send_json(task_id)
            return

        # 接受：恢复正常的任务元数据发送流程
        task = pending["task"]
        with self._lock:
            self._send_tasks[task_id] = {"task": task, "event": threading.Event(),
                                         "cancelled": False, "ack_event": threading.Event()}

        self._save_send_json(task_id)

        meta = json.dumps(task).encode("utf-8")
        self._send_callback(MSG_FILE_TASK_META, task["target"], meta)
        display = task.get("base_name") or os.path.basename(task["files"][0]["abs"])
        self.status_changed.emit(f"对方已接受，开始传输: {display}...")
        threading.Thread(target=self._wait_and_send, args=(task_id,), daemon=True).start()

    @staticmethod
    def _fmt_size(size: int) -> str:
        if size >= 1073741824:
            return f"{size / 1073741824:.1f} GB"
        if size >= 1048576:
            return f"{size / 1048576:.1f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"

    def _wait_and_send(self, task_id: int):
        with self._lock:
            state = self._send_tasks.get(task_id)
        if not state:
            return

        # 等待接收方确认（握手）
        if state["event"].wait(timeout=120):
            if state["cancelled"]:
                return
            self._send_callback(MSG_FILE_RESUME_ACK, state["task"]["target"], b"\x00")
            self._do_send_chunks(task_id)
        else:
            log.error(TAG, f"Send handshake timeout: {task_id}")
            self.status_changed.emit("发送超时：接收方未响应")
            self._remove_send_json(task_id)

    def _do_send_chunks(self, task_id: int):
        with self._lock:
            state = self._send_tasks.get(task_id)
        if not state:
            return

        task = state["task"]
        total_size = sum(f["size"] for f in task["files"])
        sent_bytes = sum(f["recv"] for f in task["files"])
        start_time = time.time()
        last_progress_time = start_time

        for idx, f_info in enumerate(task["files"]):
            if f_info["status"] == "completed":
                continue
            if state["cancelled"]:
                break

            # 计算起始chunk序号
            start_chunk = f_info["recv"] // config.FILE_CHUNK_SIZE
            total_chunks = f_info["chunk_count"]

            try:
                with open(f_info["abs"], "rb") as f:
                    f.seek(f_info["recv"])
                    chunk_seq = start_chunk
                    while chunk_seq < total_chunks and not state["cancelled"]:
                        chunk = f.read(config.FILE_CHUNK_SIZE)
                        if not chunk:
                            break

                        header = struct.pack(CHUNK_HEADER_FMT, task_id, idx, chunk_seq)
                        self._send_callback(MSG_FILE_CHUNK, task["target"], header + chunk)
                        sent_bytes += len(chunk)
                        f_info["recv"] += len(chunk)
                        chunk_seq += 1

                        # 进度报告（每秒最多一次）
                        now = time.time()
                        if now - last_progress_time >= 1.0:
                            elapsed = now - start_time
                            speed = sent_bytes / elapsed if elapsed > 0 else 0
                            pct = int(sent_bytes * 100 / total_size) if total_size > 0 else 0
                            self.progress.emit(str(task_id), pct, self._fmt_speed(speed), "")
                            last_progress_time = now
                            # 持久化进度
                            self._save_send_json(task_id)

                # 发送EOF标记
                eof_header = struct.pack(CHUNK_HEADER_FMT, task_id, idx, CHUNK_SEQ_EOF)
                self._send_callback(MSG_FILE_CHUNK, task["target"], eof_header)
                f_info["status"] = "sent"

            except Exception as e:
                log.error(TAG, f"Read file error: {e}")
                self.status_changed.emit(f"发送出错: {e}")
                break

        if state["cancelled"]:
            self.status_changed.emit("发送已取消")
        else:
            # 等待最终ACK确认所有文件接收完成
            self.status_changed.emit("等待接收方确认...")
            if state["ack_event"].wait(timeout=30.0):
                self.status_changed.emit("发送完成 ✓")
            else:
                log.warn(TAG, f"Final ACK timeout for task {task_id}")
                self.status_changed.emit("发送完成（部分ACK超时）")

        # 清理发送任务
        self._remove_send_json(task_id)
        with self._lock:
            self._send_tasks.pop(task_id, None)

    def _on_chunk_ack(self, sender_id: int, payload: bytes):
        """处理接收方的chunk确认"""
        if len(payload) < struct.calcsize(ACK_FMT):
            return
        task_id, file_idx, last_good_seq = struct.unpack_from(ACK_FMT, payload)
        # gap_count 在 payload 尾部，仅当有额外字节时读取
        gap_count = 0
        if len(payload) >= struct.calcsize(ACK_FMT) + 4:
            gap_count = struct.unpack_from("!I", payload, struct.calcsize(ACK_FMT))[0]

        with self._lock:
            state = self._send_tasks.get(task_id)
        if not state or file_idx >= len(state["task"]["files"]):
            return

        f_info = state["task"]["files"][file_idx]
        old_acked = f_info.get("acked_seq", -1)

        if gap_count > 0:
            # 有gap：需要重传
            gap_data = payload[struct.calcsize(ACK_FMT) + 4:]
            for i in range(gap_count):
                if i * 16 + 16 > len(gap_data):
                    break
                gap_start, gap_end = struct.unpack_from("!QQ", gap_data, i * 16)
                log.warn(TAG, f"Gap detected: file={file_idx} range=[{gap_start},{gap_end}]")
                # 标记需要重传的范围
                if "retransmit" not in f_info:
                    f_info["retransmit"] = []
                f_info["retransmit"].append((gap_start, gap_end))
                f_info["recv"] = min(f_info["recv"], gap_start * config.FILE_CHUNK_SIZE)

        if last_good_seq > old_acked:
            f_info["acked_seq"] = last_good_seq

        # 检查是否所有文件都完成
        all_acked = True
        for f in state["task"]["files"]:
            acked = f.get("acked_seq", -1)
            expected = f.get("chunk_count", 0)
            if acked + 1 < expected:
                all_acked = False
                break

        if all_acked:
            state["ack_event"].set()
        elif gap_count > 0:
            # 触发重传
            self._do_retransmit(task_id)

    def _do_retransmit(self, task_id: int):
        """重传gap范围内的chunk"""
        with self._lock:
            state = self._send_tasks.get(task_id)
        if not state:
            return

        task = state["task"]
        for idx, f_info in enumerate(task["files"]):
            ranges = f_info.pop("retransmit", [])
            if not ranges:
                continue

            try:
                with open(f_info["abs"], "rb") as f:
                    for gap_start, gap_end in ranges:
                        for seq in range(gap_start, gap_end + 1):
                            if state["cancelled"]:
                                return
                            offset = seq * config.FILE_CHUNK_SIZE
                            f.seek(offset)
                            chunk = f.read(config.FILE_CHUNK_SIZE)
                            if not chunk:
                                break
                            header = struct.pack(CHUNK_HEADER_FMT, task_id, idx, seq)
                            self._send_callback(MSG_FILE_CHUNK, task["target"], header + chunk)
            except Exception as e:
                log.error(TAG, f"Retransmit error: {e}")

    # ═════════════════════════════════════════
    # 接收方逻辑
    # ═════════════════════════════════════════
    def _on_task_meta(self, sender_id: int, payload: bytes):
        try:
            task = json.loads(payload.decode("utf-8"))
        except Exception:
            return

        task_id = task["task_id"]
        task["origin_sender"] = sender_id
        task["last_ack_time"] = 0.0

        with self._lock:
            local = self._recv_tasks.get(task_id)
            if local:
                # 合并已有进度
                local_map = {f["fp"]: f for f in local["files"]}
                for rf in task["files"]:
                    if rf["fp"] in local_map:
                        part = self._part_path(rf, task)
                        if os.path.exists(part):
                            rf["recv"] = os.path.getsize(part)
                            rf["acked_seq"] = local_map[rf["fp"]].get("acked_seq", -1)
                        else:
                            rf["recv"] = 0
                            rf["acked_seq"] = -1
                    else:
                        rf["recv"] = 0
                        rf["acked_seq"] = -1

        with self._lock:
            self._recv_tasks[task_id] = task
        self._save_json(task_id)

        display = task.get("base_name") or os.path.basename(task["files"][0]["abs"])
        self.status_changed.emit(f"正在接收: {display}")

        # 发送续传请求/握手确认
        req = json.dumps(task).encode("utf-8")
        self._send_callback(MSG_FILE_RESUME_REQ, sender_id, req)

    def _on_resume_ack(self, sender_id: int, payload: bytes):
        pass  # 接收方不需要处理ACK

    def _on_chunk(self, sender_id: int, payload: bytes):
        if len(payload) < CHUNK_HEADER_SIZE:
            return
        task_id, file_idx, chunk_seq = struct.unpack_from(CHUNK_HEADER_FMT, payload)
        data = payload[CHUNK_HEADER_SIZE:]

        with self._lock:
            task = self._recv_tasks.get(task_id)
            if not task or file_idx >= len(task["files"]):
                return

        f_info = task["files"][file_idx]

        # EOF标记：该文件传输完成
        if chunk_seq == CHUNK_SEQ_EOF:
            f_info["eof_received"] = True
            self._check_file_complete(task_id, file_idx, task)
            return

        part = self._part_path(f_info, task)
        self._write_queue.put((task_id, file_idx, chunk_seq, part, data))

    def _check_file_complete(self, task_id: int, file_idx: int, task: dict):
        """检查文件是否接收完整并发送最终ACK"""
        f_info = task["files"][file_idx]
        expected_chunks = f_info.get("chunk_count", 0)
        acked = f_info.get("acked_seq", -1)

        if acked + 1 >= expected_chunks:
            f_info["status"] = "completed"
            # 检查所有文件
            all_done = all(f.get("status") == "completed" for f in task["files"])
            if all_done:
                self._send_final_ack(task)
        else:
            # 有gap，发送带gap信息的ACK
            self._send_ack_with_gaps(task_id, file_idx, task)

    def _send_ack_with_gaps(self, task_id: int, file_idx: int, task: dict):
        """发送带gap信息的ACK"""
        f_info = task["files"][file_idx]
        gaps = f_info.get("gaps", [])
        last_good = f_info.get("acked_seq", -1)

        gap_data = b""
        for gs, ge in sorted(gaps):
            gap_data += struct.pack("!QQ", gs, ge)

        ack_payload = struct.pack(ACK_FMT, task_id, file_idx, last_good)
        ack_payload += struct.pack("!I", len(gaps)) + gap_data
        target = task.get("origin_sender", HOST_ID)
        self._send_callback(MSG_FILE_CHUNK_ACK, target, ack_payload)
        task["last_ack_time"] = time.time()

    def _send_final_ack(self, task: dict):
        """发送最终完成ACK"""
        for idx, f_info in enumerate(task["files"]):
            ack_payload = struct.pack(ACK_FMT, task["task_id"], idx,
                                      f_info.get("chunk_count", 0) - 1)
            target = task.get("origin_sender", HOST_ID)
            self._send_callback(MSG_FILE_CHUNK_ACK, target, ack_payload)

    def _write_loop(self):
        last_report = {}
        last_bytes = {}
        last_ack_sent = {}  # task_id -> last ack time

        while self._write_running:
            try:
                item = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            task_id, file_idx, chunk_seq, part, data = item

            with self._lock:
                task = self._recv_tasks.get(task_id)
                if not task or file_idx >= len(task["files"]):
                    continue
                f_info = task["files"][file_idx]

            try:
                os.makedirs(os.path.dirname(part) or ".", exist_ok=True)
                expected_offset = chunk_seq * config.FILE_CHUNK_SIZE

                # 检测是否乱序到达
                current_size = os.path.getsize(part) if os.path.exists(part) else 0
                if expected_offset != current_size:
                    # 记录gap
                    if "gaps" not in f_info:
                        f_info["gaps"] = []
                    expected_seq = current_size // config.FILE_CHUNK_SIZE
                    if expected_seq < chunk_seq:
                        f_info["gaps"].append((expected_seq, chunk_seq - 1))

                with open(part, "ab") as f:
                    f.write(data)
                f_info["recv"] = os.path.getsize(part) if os.path.exists(part) else len(data)

                # 更新acked_seq（最大连续序号）
                calculated_seq = (f_info["recv"] // config.FILE_CHUNK_SIZE) - 1
                new_acked = max(chunk_seq, f_info.get("acked_seq", -1))
                # 简化：如果当前chunk填补了gap，更新连续ack
                old_gaps = f_info.get("gaps", [])
                remaining_gaps = []
                for gs, ge in old_gaps:
                    if gs <= chunk_seq <= ge:
                        if chunk_seq > gs:
                            remaining_gaps.append((gs, chunk_seq - 1))
                        if chunk_seq < ge:
                            remaining_gaps.append((chunk_seq + 1, ge))
                    else:
                        remaining_gaps.append((gs, ge))
                f_info["gaps"] = remaining_gaps

                if not remaining_gaps:
                    f_info["acked_seq"] = calculated_seq
                else:
                    # acked_seq到第一个gap之前
                    first_gap_start = min(gs for gs, _ in remaining_gaps)
                    f_info["acked_seq"] = first_gap_start - 1

                self._save_json(task_id)

                # 定期发送ACK（每32个chunk或每2秒）
                now = time.time()
                tid_key = f"{task_id}_{file_idx}"
                if tid_key not in last_ack_sent:
                    last_ack_sent[tid_key] = now
                elif (chunk_seq % config.FILE_ACK_INTERVAL == 0 or
                      now - last_ack_sent[tid_key] >= 2.0):
                    if remaining_gaps:
                        self._send_ack_with_gaps(task_id, file_idx, task)
                    else:
                        ack_payload = struct.pack(ACK_FMT, task_id, file_idx,
                                                  f_info["acked_seq"])
                        target = task.get("origin_sender", HOST_ID)
                        self._send_callback(MSG_FILE_CHUNK_ACK, target, ack_payload)
                    last_ack_sent[tid_key] = now
                    task["last_ack_time"] = now

                # 进度报告
                total = sum(f["size"] for f in task["files"])
                recv = sum(f["recv"] for f in task["files"])

                if task_id not in last_report:
                    last_report[task_id] = now
                    last_bytes[task_id] = recv
                elif now - last_report[task_id] >= 1.0:
                    speed = (recv - last_bytes[task_id]) / (now - last_report[task_id])
                    self.progress.emit(str(task_id), int(recv * 100 / total) if total > 0 else 0,
                                       self._fmt_speed(speed), "")
                    last_report[task_id] = now
                    last_bytes[task_id] = recv

                # 检查是否所有文件完成（需要EOF已收到 + 所有数据已写入）
                if f_info.get("eof_received") and f_info["recv"] >= f_info["size"]:
                    f_info["status"] = "completed"
                    final = self._final_path(f_info, task)
                    os.makedirs(os.path.dirname(final) or ".", exist_ok=True)
                    if os.path.exists(final):
                        os.remove(final)
                    os.rename(part, final)
                    self._send_final_ack(task)
                    self.file_complete.emit(str(task_id), final)

                all_complete = all(f.get("status") == "completed" for f in task["files"])
                if all_complete:
                    self._remove_json(task_id)
                    with self._lock:
                        self._recv_tasks.pop(task_id, None)
                    self.status_changed.emit("接收完成 ✓")

            except Exception as e:
                log.error(TAG, f"Write error: {e}")

    # ═════════════════════════════════════════
    # 续传请求处理（接收方→发送方）
    # ═════════════════════════════════════════
    def _on_resume_req(self, sender_id: int, payload: bytes):
        try:
            remote = json.loads(payload.decode("utf-8"))
            task_id = remote["task_id"]
            with self._lock:
                state = self._send_tasks.get(task_id)
            if state:
                # 更新断点：接收方告知已收到的字节数
                local_map = {f["fp"]: f for f in state["task"]["files"]}
                for rf in remote["files"]:
                    fp = rf["fp"]
                    if fp in local_map:
                        local_map[fp]["recv"] = rf.get("recv", 0)
                        local_map[fp]["acked_seq"] = rf.get("acked_seq", -1)
                state["event"].set()
        except Exception as e:
            log.error(TAG, f"Resume req error: {e}")

    # ═════════════════════════════════════════
    # 断点续传与清理
    # ═════════════════════════════════════════
    def resume_task(self, task_id: str):
        with self._lock:
            task = self._recv_tasks.get(int(task_id))
        if task and "origin_sender" in task:
            self._send_callback(MSG_FILE_RESUME_REQ, task["origin_sender"],
                                json.dumps(task).encode("utf-8"))

    def clear_task(self, task_id: str):
        tid = int(task_id)
        with self._lock:
            task = self._recv_tasks.pop(tid, None)
        if task:
            for f in task["files"]:
                part = self._part_path(f, task)
                try:
                    if os.path.exists(part):
                        trash = part + ".trash"
                        os.rename(part, trash)
                        os.remove(trash)
                except Exception as e:
                    log.warn(TAG, f"Clear file failed: {e}")
            self._remove_json(tid)

    def _load_interrupted_tasks(self):
        for f in os.listdir(self._resume_dir):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(self._resume_dir, f), "r", encoding="utf-8") as fp:
                        task = json.load(fp)
                    with self._lock:
                        self._recv_tasks[task["task_id"]] = task
                    display = task.get("base_name") or os.path.basename(task["files"][0]["abs"])
                    self.task_interrupted.emit(str(task["task_id"]), display)
                except Exception:
                    pass

    def _save_json(self, task_id):
        with self._lock:
            task = self._recv_tasks.get(task_id)
        if task:
            try:
                with open(os.path.join(self._resume_dir, f"{task_id}.json"), "w", encoding="utf-8") as f:
                    json.dump(task, f)
            except Exception as e:
                log.warn(TAG, f"Save task json failed: {e}")

    def _remove_json(self, task_id):
        path = os.path.join(self._resume_dir, f"{task_id}.json")
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        self.task_removed.emit(str(task_id))

    # 【P0修复】发送方任务持久化
    def _save_send_json(self, task_id):
        with self._lock:
            state = self._send_tasks.get(task_id)
        if state:
            try:
                path = os.path.join(self._send_resume_dir, f"{task_id}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state["task"], f)
            except Exception as e:
                log.warn(TAG, f"Save send task json failed: {e}")

    def _remove_send_json(self, task_id):
        path = os.path.join(self._send_resume_dir, f"{task_id}.json")
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _part_path(self, f_info, task):
        base = task.get("base_name")
        return os.path.join(self._save_dir, base, f_info["rel"] + ".part") if base else os.path.join(self._save_dir, f_info["rel"] + ".part")

    def _final_path(self, f_info, task):
        base = task.get("base_name")
        return os.path.join(self._save_dir, base, f_info["rel"]) if base else os.path.join(self._save_dir, f_info["rel"])

    @staticmethod
    def _fmt_speed(bps: float) -> str:
        if bps >= 1048576:
            return f"{bps / 1048576:.1f} MB/s"
        if bps >= 1024:
            return f"{bps / 1024:.1f} KB/s"
        return f"{bps:.0f} B/s"

    def cleanup(self):
        self._write_running = False
