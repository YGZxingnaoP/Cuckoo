# -*- coding: utf-8 -*-
"""
接收写盘工作线程
───────────────────────────────────────────
全局单例接收写盘线程：大文件按 seq 随机写，小文件追加写。
大文件 seq 完整性检查 + MD5 校验 + 重传请求。
"""

import os
import json
import threading
import time
import queue
import traceback
from typing import Callable

from common import logger as log
from core.protocol import (
    MSG_FILE_CHUNK_ACK, MSG_FILE_RETRANSMIT_REQ, HOST_ID,
)
import config

from func.file_transfer.common import (
    TAG, ACK_STRUCT, EOF_SEQ,
    _BitSet, fmt_speed, is_large_file, calc_file_md5,
)


class _RecvWorker:
    """
    全局单例接收写盘线程。
    大文件用 seq 随机写（seek+write），小文件追加写。
    """

    def __init__(
        self,
        base_dir: str,
        lock: threading.RLock,
        send_cb: Callable[[int, int, bytes], None],
        on_progress: Callable[[str, int, str, str], None],
        on_file_complete: Callable[[str, str], None],
        on_task_complete: Callable[[int], None],
        on_status: Callable[[str], None],
        on_checkpoint: Callable[[int], None],
    ):
        self._base_dir = base_dir
        self._lock = lock
        self._send_cb = send_cb
        self._on_progress = on_progress
        self._on_file_complete = on_file_complete
        self._on_task_complete = on_task_complete
        self._on_status = on_status
        self._on_checkpoint = on_checkpoint

        self._queue: queue.Queue = queue.Queue(maxsize=config.FILE_WRITE_QUEUE_MAX)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="FileWriteWorker")
        self._thread.start()

        # 断点续传：每处理 N 个数据块触发一次进度落盘
        self._chunk_counter = 0

        self._last_report: dict[int, tuple[float, int]] = {}
        self._handles: dict[str, object] = {}
        self._handles_lock = threading.Lock()

    def stop(self) -> None:
        self._running = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        with self._handles_lock:
            for fh in self._handles.values():
                try:
                    fh.close()
                except OSError:
                    pass
            self._handles.clear()

    def close_handle(self, path: str) -> None:
        with self._handles_lock:
            fh = self._handles.pop(path, None)
        if fh is not None:
            try:
                fh.flush()
                fh.close()
            except OSError:
                pass

    def enqueue(self, item: tuple) -> None:
        if not self._running:
            return
        # 阻塞入队（正确背压），但定期检查是否已停止，防止永久死锁
        while self._running:
            try:
                self._queue.put(item, timeout=5.0)
                return
            except queue.Full:
                if not self._running:
                    return
                # 仍在运行，继续阻塞等待写盘线程消费

    def _run(self) -> None:
        while self._running:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                continue
            tid, idx, target_path, seq, data, fi, task = item
            self._process_item(tid, idx, target_path, seq, data, fi, task)

    def _process_item(
        self, tid: int, idx: int, target_path: str, seq: int, data: bytes,
        fi: dict, task: dict
    ) -> None:
        try:
            parent = os.path.dirname(target_path) or "."
            os.makedirs(parent, exist_ok=True)

            large = is_large_file(fi)

            if data:
                if large:
                    # 大文件：按 seq 随机写（保证重传/乱序正确落位）
                    with self._handles_lock:
                        fh = self._handles.get(target_path)
                        if fh is None or fh.closed:
                            fh = open(target_path, "r+b") if os.path.exists(target_path) else open(target_path, "w+b")
                            self._handles[target_path] = fh
                    fh.seek(seq * config.FILE_CHUNK_SIZE)
                    fh.write(data)
                    with self._lock:
                        bs = fi.get("received_seqs")
                        if bs is None:
                            bs = _BitSet(fi.get("chunk_count", 0))
                            fi["received_seqs"] = bs
                        bs.add(seq)
                        fi["recv"] = min(bs.count() * config.FILE_CHUNK_SIZE, fi["size"])
                        # 【bug修复】大文件 ACK 用连续前缀，缺口后不虚高（保证蓄水池正确）
                        fi["acked_seq"] = bs.contiguous_prefix() - 1
                else:
                    # 小文件：追加写
                    with self._handles_lock:
                        fh = self._handles.get(target_path)
                        if fh is None or fh.closed:
                            fh = open(target_path, "ab")
                            self._handles[target_path] = fh
                    fh.write(data)
                    with self._lock:
                        fi["recv"] = fi.get("recv", 0) + len(data)
                        fi["acked_seq"] = max(0, (fi["recv"] // config.FILE_CHUNK_SIZE) - 1)

            # ── 进度报告 ──
            self._report_progress(tid, task)

            # ── 定期 ACK（所有文件都发，大文件也需 ACK 驱动蓄水池流控） ──
            self._maybe_send_ack(tid, idx, fi, task)

            # ── 断点续传：每处理 50 个数据块落盘一次进度 ──
            if data:
                self._chunk_counter += 1
                if self._chunk_counter >= 50:
                    self._chunk_counter = 0
                    self._on_checkpoint(tid)

            # ── 完成检查（仅 EOF/VERIFY 触发，避免大文件每次写盘都做 O(n) 集合差集） ──
            # 写队列 FIFO 保证 EOF 在所有 chunk 之后处理，此时 received_seqs 已完整
            if seq == EOF_SEQ:
                self._check_file_complete(tid, idx, target_path, fi, task)

        except Exception as e:
            log.error(TAG, f"Write worker error for task {tid}/{idx}: {e}\n{traceback.format_exc()}")

    def _report_progress(self, tid: int, task: dict) -> None:
        with self._lock:
            total = sum(f["size"] for f in task["files"])
            recv = sum(f.get("recv", 0) for f in task["files"])
        now = time.time()
        if tid not in self._last_report:
            self._last_report[tid] = (now, recv)
        elif now - self._last_report[tid][0] >= 1.0:
            prev_t, prev_b = self._last_report[tid]
            speed = (recv - prev_b) / (now - prev_t) if now > prev_t else 0
            pct = int(recv * 100 / total) if total > 0 else 0
            self._on_progress(str(tid), min(pct, 99), fmt_speed(speed), "")
            self._last_report[tid] = (now, recv)

    def _maybe_send_ack(self, tid: int, idx: int, fi: dict, task: dict) -> None:
        with self._lock:
            acked_bytes = sum(
                min(max(0, f.get("acked_seq", -1) + 1) * config.FILE_CHUNK_SIZE, f["size"])
                for f in task["files"]
            )
            last_ack_key = f"__last_ack_{tid}"
            last_ack_val = task.get(last_ack_key, 0)
            need_ack = (acked_bytes - last_ack_val >= config.FILE_ACK_THRESHOLD)
            if need_ack:
                task[last_ack_key] = acked_bytes
                ack_seq = fi["acked_seq"]
        if need_ack:
            self._send_cb(
                MSG_FILE_CHUNK_ACK,
                task.get("origin_sender", HOST_ID),
                ACK_STRUCT.pack(tid, idx, ack_seq)
            )

    def _send_retransmit_req(self, tid: int, idx: int, origin: int, seqs: list) -> None:
        """分片发送重传请求，避免超大 seq 列表导致单帧超 16MB 限制。"""
        bsize = config.FILE_META_BATCH_SIZE * 16  # 每批约 8万 seq，JSON 约 600KB
        if len(seqs) == 0:
            return
        batches = max(1, (len(seqs) + bsize - 1) // bsize)
        for i in range(batches):
            batch = seqs[i * bsize:(i + 1) * bsize]
            req = json.dumps({"task_id": tid, "file_idx": idx, "seqs": batch}).encode()
            self._send_cb(MSG_FILE_RETRANSMIT_REQ, origin, req)
        log.log(TAG, f"Recv: sent retransmit req for file idx={idx}, {len(seqs)} chunks in {batches} batch(es)")

    def _check_file_complete(self, tid: int, idx: int, target_path: str, fi: dict, task: dict) -> None:
        """检查文件是否完成。大文件需要 seq 完整 + MD5 校验通过。"""
        with self._lock:
            eof = bool(fi.get("eof_received"))
            if not eof:
                return
            large = is_large_file(fi)
            already_done = (fi.get("status") == "completed")
            if large:
                total_chunks = fi.get("chunk_count", 0)
                bs = fi.get("received_seqs")
                if bs is None:
                    bs = _BitSet(total_chunks)
                    fi["received_seqs"] = bs
                data_complete = bs.is_complete()
                missing = bs.missing() if not data_complete else []
                md5_ready = bool(fi.get("expected_md5"))
                md5_verified = bool(fi.get("md5_verified"))
                retransmit_round = fi.get("retransmit_round", 0)
                expected_md5 = fi.get("expected_md5", "")
            else:
                total_chunks = 0
                missing = []
                data_complete = fi.get("recv", 0) >= fi.get("size", 0)
                md5_ready = True
                md5_verified = True  # 小文件无需 MD5
                retransmit_round = 0
                expected_md5 = ""

        if already_done:
            return

        origin = task.get("origin_sender", HOST_ID)

        # ── 数据不完整：请求重传（仅大文件） ──
        if not data_complete:
            if large and retransmit_round < config.MAX_RETRANSMIT_ROUNDS:
                with self._lock:
                    fi["retransmit_round"] = retransmit_round + 1
                if missing:
                    self._send_retransmit_req(tid, idx, origin, missing)
            elif large:
                log.error(TAG, f"Recv: file idx={idx} still missing {len(missing)} chunks after {config.MAX_RETRANSMIT_ROUNDS} rounds")
            return

        # ── 数据完整但 MD5 未就绪（等待 VERIFY） ──
        if large and not md5_ready:
            return

        # ── 大文件 MD5 校验 ──
        if large and not md5_verified:
            # 校验前先 flush + close 写句柄，确保数据落盘
            self.close_handle(target_path)
            actual = calc_file_md5(target_path)
            if expected_md5 and actual == expected_md5:
                with self._lock:
                    fi["md5_verified"] = True
                log.log(TAG, f"Recv: MD5 verified for file idx={idx}")
                # 校验通过后继续到 _finalize_file
            else:
                log.error(TAG, f"Recv: MD5 mismatch for file idx={idx} (expected={expected_md5[:16]} actual={actual[:16]})")
                if retransmit_round < config.MAX_RETRANSMIT_ROUNDS:
                    with self._lock:
                        fi["retransmit_round"] = retransmit_round + 1
                        bs = fi.get("received_seqs")
                        if bs is not None:
                            bs.clear()
                        fi["recv"] = 0
                        fi["md5_verified"] = False
                        fi["expected_md5"] = None
                        fi["status"] = "pending"
                    all_seqs = list(range(total_chunks))
                    self._send_retransmit_req(tid, idx, origin, all_seqs)
                    self.close_handle(target_path)
                    try:
                        if os.path.exists(target_path):
                            os.remove(target_path)
                    except OSError as e:
                        log.warn(TAG, f"Remove corrupted part failed: {e}")
                    log.log(TAG, f"Recv: request full retransmit for file idx={idx} (MD5 mismatch)")
                    return
                else:
                    log.error(TAG, f"Recv: MD5 still mismatch after {config.MAX_RETRANSMIT_ROUNDS} rounds")
                    return

        # 完成文件
        self._finalize_file(tid, idx, target_path, fi, task)

    def _finalize_file(self, tid: int, idx: int, target_path: str, fi: dict, task: dict) -> None:
        """将 .part 重命名为最终文件名"""
        final_path = target_path
        if final_path.endswith(".part"):
            final_path = final_path[:-5]

        # 关闭句柄
        self.close_handle(target_path)

        if fi.get("size", 0) == 0:
            try:
                parent = os.path.dirname(final_path) or "."
                os.makedirs(parent, exist_ok=True)
                with open(final_path, "wb") as _f:
                    pass
            except OSError as e:
                log.error(TAG, f"Create empty file {final_path} failed: {e}")
                final_path = target_path
        else:
            if os.path.exists(final_path):
                try:
                    os.remove(final_path)
                except OSError as e:
                    log.warn(TAG, f"Remove existing {final_path} failed: {e}")
            try:
                os.rename(target_path, final_path)
            except OSError as e:
                log.error(TAG, f"Rename {target_path} -> {final_path} failed: {e}")
                final_path = target_path

        with self._lock:
            fi["status"] = "completed"
            fi["acked_seq"] = max(0, fi.get("chunk_count", 0) - 1)

        self._on_file_complete.emit(str(tid), final_path)
        log.log(TAG, f"Recv: file completed: {final_path}")

        # 发送最终 ACK
        self._send_cb(
            MSG_FILE_CHUNK_ACK,
            task.get("origin_sender", HOST_ID),
            ACK_STRUCT.pack(tid, idx, fi["acked_seq"])
        )

        # 任务完成检测
        with self._lock:
            all_done = all(f.get("status") == "completed" for f in task["files"])
        if all_done:
            for i, ff in enumerate(task["files"]):
                final_seq = max(0, ff.get("chunk_count", 0) - 1)
                self._send_cb(
                    MSG_FILE_CHUNK_ACK,
                    task.get("origin_sender", HOST_ID),
                    ACK_STRUCT.pack(tid, i, final_seq)
                )
            self._on_task_complete(tid)
            self._on_status.emit("接收完成 ✓")
            if tid in self._last_report:
                del self._last_report[tid]
            log.log(TAG, f"Recv: all files completed for task {tid}")
