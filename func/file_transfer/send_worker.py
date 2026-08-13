# -*- coding: utf-8 -*-
"""
发送工作线程
───────────────────────────────────────────
单个发送任务的 worker：分块读取 → 蓄水池流控 → 发送 chunk → 收集 ACK。
非空文件：MD5 → VERIFY；大文件额外：响应重传请求。
"""

import json
import threading
import time
import queue
import hashlib
import traceback
from typing import Optional, Callable

from common import logger as log
from core.protocol import (
    MSG_FILE_CHUNK, MSG_FILE_VERIFY,
)
import config

from func.file_transfer.common import (
    TAG, CHUNK_HEADER, EOF_SEQ,
    fmt_speed, is_large_file, calc_file_md5,
)


class _SendWorker:
    """
    单个发送任务的 worker。
    发送 chunk → MD5 → EOF → VERIFY；大文件额外响应重传请求。
    """

    def __init__(
        self,
        task: dict,
        send_cb: Callable[[int, int, bytes], None],
        lock: threading.RLock,
        on_progress: Callable[[str, int, str, str], None],
        on_status: Callable[[str], None],
        on_complete: Callable[[bool], None],
    ):
        self._task = task
        self._send_cb = send_cb
        self._lock = lock
        self._on_progress = on_progress
        self._on_status = on_status
        self._on_complete = on_complete

        self._tid = task["task_id"]
        self._target = task["target"]
        self._files = task["files"]
        self._total_size = sum(f["size"] for f in self._files)
        self._sent_total = sum(f.get("recv", 0) for f in self._files)

        self._cancelled = threading.Event()
        self._all_acked = threading.Event()
        self._resume_ready = threading.Event()
        # 重传请求队列: (file_idx, seqs)
        self._retransmit_queue: queue.Queue = queue.Queue()
        # 文件的 MD5（重传时需重发 VERIFY）
        self._file_md5s: dict[int, str] = {}
        # 是否成功完成（所有文件 ACK 确认）。失败时保留任务供断点续传。
        self._success = False
        # ACK/重传活动时间戳（用于空闲超时判定，替代固定总超时）
        self._last_activity = time.time()

    def signal_resume_ready(self) -> None:
        self._resume_ready.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._all_acked.set()
        self._resume_ready.set()

    def on_ack(self, file_idx: int, acked_seq: int) -> None:
        with self._lock:
            if 0 <= file_idx < len(self._files):
                fi = self._files[file_idx]
                if acked_seq > fi.get("acked_seq", -1):
                    fi["acked_seq"] = acked_seq
                    self._last_activity = time.time()
        if self._all_files_acked():
            self._all_acked.set()

    def on_retransmit_req(self, file_idx: int, seqs: list) -> None:
        self._retransmit_queue.put((file_idx, seqs))

    def _all_files_acked(self) -> bool:
        with self._lock:
            for f in self._files:
                expected = f.get("chunk_count", 0) - 1
                if f.get("acked_seq", -1) < expected:
                    return False
            return True

    def _acked_bytes(self) -> int:
        with self._lock:
            total = 0
            for f in self._files:
                acked_seq = f.get("acked_seq", -1)
                if acked_seq < 0:
                    continue
                full_bytes = (acked_seq + 1) * config.FILE_CHUNK_SIZE
                total += min(full_bytes, f["size"])
            return min(total, self._total_size)

    def run(self) -> None:
        try:
            self._do_send()
        except Exception as e:
            log.error(TAG, f"Send worker {self._tid} error: {e}\n{traceback.format_exc()}")
        finally:
            self._on_complete(self._success)

    def _do_send(self) -> None:
        # 等待 RESUME_REQ 更新续传进度。
        # 超大文件夹的 TASK_META 分片传输 + 接收端处理 + RESUME_REQ 分片回传
        # 需要更长时间，故超时设为 30 秒（兜底，超时后从头发送）。
        if not self._resume_ready.wait(timeout=30.0):
            log.warn(TAG, f"RESUME_REQ not received in 30s for tid={self._tid}, starting anyway")

        # 续传进度已在 _apply_progress_to_task 中应用，这里重新同步 sent_total
        with self._lock:
            self._sent_total = sum(f.get("recv", 0) for f in self._files)

        t0 = time.time()
        self._last_report = t0

        for idx, fi in enumerate(self._files):
            if self._cancelled.is_set():
                self._on_status.emit("传输已取消")
                return
            if fi.get("status") == "completed":
                continue

            md5_hash = self._send_one_file(idx, fi, t0)
            if md5_hash is None:
                return  # 取消或错误

            # 所有非空文件：发送 MD5 校验值，并保存以便重传时重发
            if md5_hash:
                self._file_md5s[idx] = md5_hash
                verify = json.dumps({"task_id": self._tid, "file_idx": idx, "md5": md5_hash}).encode()
                self._send_cb(MSG_FILE_VERIFY, self._target, verify)
                log.log(TAG, f"Send: file idx={idx} VERIFY md5={md5_hash[:16]}...")

        self._on_status.emit("发送完成，等待确认...")
        # 进入重传循环：响应接收方的重传请求，直到全部确认或超时
        self._retransmit_loop()

    def _pool_wait(self) -> bool:
        """蓄水池流控：在途数据超过上限时等待。返回 False 表示已取消。"""
        inflight = self._sent_total - self._acked_bytes()
        stall_start = None
        while inflight >= config.FILE_POOL_SIZE:
            if self._cancelled.is_set():
                return False
            if stall_start is None:
                stall_start = time.time()
            elif time.time() - stall_start > config.FILE_POOL_STALL_TIMEOUT:
                log.warn(TAG, f"Pool stalled, forcing continue (inflight={inflight})")
                return True
            time.sleep(0.1)
            inflight = self._sent_total - self._acked_bytes()
        return True

    def _send_one_file(self, idx: int, fi: dict, t0: float) -> Optional[str]:
        """发送单个文件的 chunk + EOF。返回 MD5（非空文件）或空字符串。"""
        size = fi.get("size", 0)
        need_md5 = size > 0
        md5 = hashlib.md5() if need_md5 else None
        total_chunks = fi.get("chunk_count", 0)

        if total_chunks == 0:
            # 空文件：只需发送 EOF
            header = CHUNK_HEADER.pack(self._tid, idx, EOF_SEQ)
            self._send_cb(MSG_FILE_CHUNK, self._target, header)
            fi["status"] = "sent"
            return ""

        # ── 大文件续传：只发送缺失的 seq（随机 seek），幂等补全 ──
        missing = fi.get("_missing_seqs")
        if missing is not None and is_large_file(fi):
            return self._send_missing_chunks(idx, fi, missing)

        # ── 小文件续传且数据已完整：只补发 EOF + VERIFY，不重发数据块 ──
        if fi.get("recv", 0) >= size > 0:
            header = CHUNK_HEADER.pack(self._tid, idx, EOF_SEQ)
            self._send_cb(MSG_FILE_CHUNK, self._target, header)
            fi["status"] = "sent"
            log.log(TAG, f"Send: file idx={idx} already complete, resending EOF+VERIFY")
            return calc_file_md5(fi["abs"])

        start_chunk = fi.get("recv", 0) // config.FILE_CHUNK_SIZE

        try:
            with open(fi["abs"], "rb") as fh:
                fh.seek(fi.get("recv", 0))
                seq = start_chunk
                while seq < total_chunks:
                    if self._cancelled.is_set():
                        self._on_status.emit("传输已取消")
                        return None

                    if not self._pool_wait():
                        return None

                    data = fh.read(config.FILE_CHUNK_SIZE)
                    if not data:
                        break

                    header = CHUNK_HEADER.pack(self._tid, idx, seq)
                    self._send_cb(MSG_FILE_CHUNK, self._target, header + data)

                    if md5 is not None:
                        md5.update(data)

                    with self._lock:
                        self._sent_total += len(data)
                        fi["recv"] = fi.get("recv", 0) + len(data)

                    seq += 1

                    now = time.time()
                    if now - self._last_report >= 1.0:
                        self._report_progress(t0, now)
                        self._last_report = now

            # 发送 EOF
            header = CHUNK_HEADER.pack(self._tid, idx, EOF_SEQ)
            self._send_cb(MSG_FILE_CHUNK, self._target, header)
            fi["status"] = "sent"
            log.log(TAG, f"Send: file idx={idx} sent, {fi['recv']} bytes")

            # 续传场景：流式 MD5 只覆盖后半部分，需重新计算完整文件 MD5。
            # 注意必须用 recv 而非 start_chunk 判断：recv 可能小于一个 chunk
            # （如 65535 字节），此时 start_chunk 仍为 0，但流式 MD5 已不完整。
            if need_md5 and fi.get("recv", 0) > 0:
                return calc_file_md5(fi["abs"])
            return md5.hexdigest() if md5 else ""

        except FileNotFoundError:
            log.error(TAG, f"File not found: {fi['abs']}")
            self._on_status.emit(f"文件不存在: {fi['abs']}")
            return None
        except OSError as e:
            log.error(TAG, f"Read error for {fi['abs']}: {e}")
            self._on_status.emit(f"读取文件失败: {e}")
            return None

    def _send_missing_chunks(self, idx: int, fi: dict, missing: list) -> Optional[str]:
        """大文件续传：只发送缺失的 seq，最后发 EOF，并返回重新计算的完整文件 MD5。"""
        try:
            with open(fi["abs"], "rb") as fh:
                for seq in missing:
                    if self._cancelled.is_set():
                        self._on_status.emit("传输已取消")
                        return None
                    if not self._pool_wait():
                        return None
                    offset = seq * config.FILE_CHUNK_SIZE
                    fh.seek(offset)
                    data = fh.read(config.FILE_CHUNK_SIZE)
                    if not data:
                        break
                    header = CHUNK_HEADER.pack(self._tid, idx, seq)
                    self._send_cb(MSG_FILE_CHUNK, self._target, header + data)
                    with self._lock:
                        self._sent_total += len(data)
                        # 【bug修复】同步更新 recv（进度准确性）
                        fi["recv"] = min(fi.get("recv", 0) + len(data), fi["size"])

            # 发送 EOF，让接收方重新检查完整性
            header = CHUNK_HEADER.pack(self._tid, idx, EOF_SEQ)
            self._send_cb(MSG_FILE_CHUNK, self._target, header)
            fi["status"] = "sent"
            # 【bug修复】清除缺失标记，避免重复续传
            fi["_missing_seqs"] = None
            log.log(TAG, f"Send: file idx={idx} resume-sent {len(missing)} missing chunks")

            # 返回完整文件 MD5（接收方补全后需校验）
            return calc_file_md5(fi["abs"])

        except FileNotFoundError:
            log.error(TAG, f"File not found: {fi['abs']}")
            self._on_status.emit(f"文件不存在: {fi['abs']}")
            return None
        except OSError as e:
            log.error(TAG, f"Read error for {fi['abs']}: {e}")
            self._on_status.emit(f"读取文件失败: {e}")
            return None

    def _retransmit_loop(self) -> None:
        """响应重传请求，直到全部文件确认完成或空闲超时。

        使用"空闲超时"而非固定总超时：只要仍有 ACK 或重传活动，就一直等待，
        避免超大文件 / 慢速接收端在固定 60 秒后被误判为"部分未确认"。
        """
        self._last_activity = time.time()
        idle_timeout = config.FILE_ACK_FINAL_TIMEOUT

        while True:
            if self._cancelled.is_set():
                return
            if self._all_files_acked():
                self._success = True  # 所有文件确认完成
                self._on_status.emit("发送完成 ✓")
                return

            if time.time() - self._last_activity > idle_timeout:
                break

            try:
                file_idx, seqs = self._retransmit_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            log.log(TAG, f"Retransmit: file idx={file_idx}, {len(seqs)} chunks")
            self._resend_chunks(file_idx, seqs)
            self._last_activity = time.time()

        log.warn(TAG, f"ACK/retransmit idle timeout for task {self._tid}")
        self._on_status.emit("发送完成（部分未确认）")

    def _resend_chunks(self, file_idx: int, seqs: list) -> None:
        """重发指定 seq 的 chunk，最后重发 EOF 和 VERIFY（大文件）"""
        if file_idx >= len(self._files):
            return
        fi = self._files[file_idx]
        try:
            with open(fi["abs"], "rb") as fh:
                for seq in seqs:
                    if self._cancelled.is_set():
                        return
                    offset = seq * config.FILE_CHUNK_SIZE
                    fh.seek(offset)
                    data = fh.read(config.FILE_CHUNK_SIZE)
                    if not data:
                        break
                    header = CHUNK_HEADER.pack(self._tid, file_idx, seq)
                    self._send_cb(MSG_FILE_CHUNK, self._target, header + data)
                    # 【bug修复】重传同样更新在途字节与文件进度，保证蓄水池/进度统计准确
                    with self._lock:
                        self._sent_total += len(data)
                        fi["recv"] = min(fi.get("recv", 0) + len(data), fi["size"])
            # 重发 EOF，让接收方重新检查完整性
            header = CHUNK_HEADER.pack(self._tid, file_idx, EOF_SEQ)
            self._send_cb(MSG_FILE_CHUNK, self._target, header)
            # 重发 VERIFY，让接收方重新校验 MD5（尤其整体重传场景）
            md5_hash = self._file_md5s.get(file_idx)
            if md5_hash:
                verify = json.dumps({"task_id": self._tid, "file_idx": file_idx, "md5": md5_hash}).encode()
                self._send_cb(MSG_FILE_VERIFY, self._target, verify)
                log.log(TAG, f"Retransmit: resend VERIFY for file idx={file_idx}")
        except OSError as e:
            log.error(TAG, f"Resend error for file {file_idx}: {e}")

    def _report_progress(self, t0: float, now: float) -> None:
        elapsed = now - t0
        speed = self._sent_total / elapsed if elapsed > 0 else 0
        pct = int(self._sent_total * 100 / self._total_size) if self._total_size > 0 else 0
        self._on_progress.emit(str(self._tid), min(pct, 99), fmt_speed(speed), "")
