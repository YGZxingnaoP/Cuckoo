# -*- coding: utf-8 -*-
"""
统一文件传输管理器
───────────────────────────────────────────
协调发送/接收/邀约/续传/持久化。
对外 API 保持不变，供 UI 层调用。
"""

import os
import json
import copy
import threading
import time
import hashlib
from typing import Callable

from PySide6.QtCore import QObject, Signal
from common import logger as log
from core.protocol import (
    MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK,
    MSG_FILE_CHUNK, MSG_FILE_CHUNK_ACK, MSG_FILE_OFFER, MSG_FILE_OFFER_RESP,
    MSG_FILE_CANCEL, MSG_FILE_RETRANSMIT_REQ, MSG_FILE_VERIFY, HOST_ID,
)
import config

from func.file_transfer.common import (
    TAG, CHUNK_HEADER, ACK_STRUCT, CANCEL_FMT, EOF_SEQ,
    generate_task_id, fmt_size, sanitize_rel_path, is_large_file,
    _BitSet, _TaskStore,
)
from func.file_transfer.send_worker import _SendWorker
from func.file_transfer.recv_worker import _RecvWorker


class UnifiedFileTransfer(QObject):

    progress = Signal(str, int, str, str)
    file_complete = Signal(str, str)
    status_changed = Signal(str)
    task_interrupted = Signal(str, str)
    task_removed = Signal(str)
    file_offer_received = Signal(str, str, str)

    def __init__(self, my_id: int, send_callback: Callable[[int, int, bytes], None]):
        super().__init__()
        self._my_id = my_id
        self._send_cb = send_callback
        self._base_dir = config.DOWNLOAD_DIR
        os.makedirs(self._base_dir, exist_ok=True)

        self._lock = threading.RLock()
        self._store = _TaskStore(self._base_dir)

        self._recv_tasks: dict[int, dict] = {}
        self._send_tasks: dict[int, dict] = {}
        self._pending_offers: dict[int, dict] = {}
        self._received_offers: dict[int, dict] = {}
        # 元数据分片累积缓冲（解决极大文件数导致单帧超 16MB 限制）
        self._meta_buffers: dict[int, dict] = {}    # 接收端：TASK_META 分片
        self._resume_buffers: dict[int, dict] = {}  # 发送端：RESUME_REQ 分片

        self._recv_worker = _RecvWorker(
            base_dir=self._base_dir,
            lock=self._lock,
            send_cb=self._send_cb,
            on_progress=self._on_recv_progress,
            on_file_complete=self.file_complete,
            on_task_complete=self._on_recv_task_complete,
            on_status=self.status_changed,
            on_checkpoint=self._on_recv_checkpoint,
        )

        self._load_interrupted_tasks()

    # ══════════════════════════════════════════
    # 公共 API
    # ══════════════════════════════════════════

    def handle_incoming(self, msg_type: int, sender_id: int, payload: bytes) -> None:
        handlers = {
            MSG_FILE_OFFER:           self._handle_offer,
            MSG_FILE_OFFER_RESP:      self._handle_offer_resp,
            MSG_FILE_TASK_META:       self._handle_task_meta,
            MSG_FILE_RESUME_REQ:      self._handle_resume_req,
            MSG_FILE_RESUME_ACK:      self._handle_resume_ack,
            MSG_FILE_CHUNK:           self._handle_chunk,
            MSG_FILE_CHUNK_ACK:       self._handle_ack,
            MSG_FILE_CANCEL:          self._handle_cancel,
            MSG_FILE_RETRANSMIT_REQ:  self._handle_retransmit_req,
            MSG_FILE_VERIFY:          self._handle_verify,
        }
        handler = handlers.get(msg_type)
        if handler:
            handler(sender_id, payload)
        else:
            log.warn(TAG, f"Unknown file msg_type: 0x{msg_type:02X}")

    def send_file(self, path: str, target_id: int) -> None:
        log.log(TAG, f"send_file: {path} → target={target_id}")
        self._start_send([path], target_id, is_folder=False)

    def send_folder(self, dir_path: str, target_id: int) -> None:
        log.log(TAG, f"send_folder: {dir_path} → target={target_id}")
        files = []
        for root, _, filenames in os.walk(dir_path):
            for fn in filenames:
                files.append(os.path.join(root, fn))
        if not files:
            self.status_changed.emit("文件夹为空")
            return
        base = os.path.basename(dir_path)
        self._start_send(files, target_id, is_folder=True, base_name=base, root_dir=dir_path)

    def respond_to_offer(self, task_id: str, accept: bool) -> None:
        try:
            tid = int(task_id)
        except (ValueError, TypeError):
            log.error(TAG, f"respond_to_offer: invalid task_id={task_id!r}")
            return

        with self._lock:
            pkg = self._received_offers.pop(tid, None)
        if not pkg:
            log.warn(TAG, f"respond_to_offer: task {tid} not found")
            return

        offer = pkg["offer"]
        sid = pkg["sid"]
        resp = json.dumps({"task_id": tid, "accept": accept}).encode()
        flag = b"\x01" if accept else b"\x00"
        self._send_cb(MSG_FILE_OFFER_RESP, sid, flag + resp)
        log.log(TAG, f"respond_to_offer: task={tid} accepted={accept} → sid={sid}")

        display = offer.get("display_name", "")
        self.status_changed.emit(f"{'已接受' if accept else '已拒绝'}: {display}")

    def resume_task(self, task_id: str) -> None:
        try:
            tid = int(task_id)
        except (ValueError, TypeError):
            log.error(TAG, f"resume_task: invalid task_id={task_id!r}")
            return

        # 【bug修复】锁内快照，避免锁外遍历时写盘线程并发修改导致数据竞争
        with self._lock:
            task = self._recv_tasks.get(tid)
            if task:
                snapshot = copy.deepcopy(task)
            else:
                snapshot = None

        if snapshot and "origin_sender" in snapshot:
            self._send_resume_req_batches(snapshot, snapshot["origin_sender"])

    def clear_task(self, task_id: str) -> None:
        try:
            tid = int(task_id)
        except (ValueError, TypeError):
            log.error(TAG, f"clear_task: invalid task_id={task_id!r}")
            return

        with self._lock:
            task = self._recv_tasks.pop(tid, None)
        if not task:
            return

        for fi in task["files"]:
            part_path = self._build_part_path(fi, task)
            self._recv_worker.close_handle(part_path)
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError as e:
                log.warn(TAG, f"Remove part file failed: {part_path}: {e}")

        self._store.delete_recv(tid)
        self.task_removed.emit(str(tid))

    def cancel_task(self, task_id: str) -> None:
        try:
            tid = int(task_id)
        except (ValueError, TypeError):
            log.error(TAG, f"cancel_task: invalid task_id={task_id!r}")
            return

        with self._lock:
            send_entry = self._send_tasks.get(tid)
        if send_entry:
            worker = send_entry.get("worker")
            if worker:
                worker.cancel()
            task = send_entry.get("task", {})
            target = task.get("target", 0)
            self._send_cb(MSG_FILE_CANCEL, target, CANCEL_FMT.pack(tid))
            with self._lock:
                self._send_tasks.pop(tid, None)
            self._store.delete_send(tid)
            self.status_changed.emit("传输已取消")
            return

        with self._lock:
            recv_task = self._recv_tasks.get(tid)
        if recv_task:
            origin = recv_task.get("origin_sender", HOST_ID)
            self._send_cb(MSG_FILE_CANCEL, origin, CANCEL_FMT.pack(tid))
            self.clear_task(str(tid))
            self.status_changed.emit("接收已取消")
            return

        log.warn(TAG, f"cancel_task: task {tid} not found")

    def cleanup(self) -> None:
        with self._lock:
            for entry in list(self._send_tasks.values()):
                worker = entry.get("worker")
                if worker:
                    worker.cancel()
            self._send_tasks.clear()
            self._meta_buffers.clear()
            self._resume_buffers.clear()
        if self._recv_worker:
            self._recv_worker.stop()

    # ══════════════════════════════════════════
    # 发送端内部方法
    # ══════════════════════════════════════════

    def _start_send(
        self, paths: list, target_id: int,
        is_folder: bool, base_name: str = "", root_dir: str = ""
    ) -> None:
        tid = generate_task_id()
        files = []
        total_size = 0

        for p in paths:
            try:
                sz = os.path.getsize(p)
            except OSError as e:
                log.warn(TAG, f"Skip {p}: {e}")
                continue

            fingerprint = hashlib.md5(f"{sz}_{os.path.getmtime(p)}".encode()).hexdigest()[:8]
            try:
                rel = sanitize_rel_path(
                    os.path.relpath(p, root_dir) if root_dir else os.path.basename(p)
                )
            except ValueError as e:
                log.warn(TAG, f"Path rejected: {e}")
                continue

            files.append({
                "abs":         p.replace("\\", "/"),
                "rel":         rel,
                "size":        sz,
                "fp":          fingerprint,
                "recv":        0,
                "status":      "pending",
                "chunk_count": (sz + config.FILE_CHUNK_SIZE - 1) // config.FILE_CHUNK_SIZE,
                "acked_seq":   -1,
            })
            total_size += sz

        if not files:
            self.status_changed.emit("无可发送的文件")
            return

        display_name = base_name or os.path.basename(files[0]["abs"])
        size_desc = f"文件夹 · {len(files)} 个文件" if is_folder else fmt_size(total_size)

        offer = {
            "task_id":      tid,
            "sender":       self._my_id,
            "target":       target_id,
            "is_folder":    is_folder,
            "base_name":    base_name,
            "display_name": display_name,
            "size_desc":    size_desc,
            "total_size":   total_size,
            "file_count":   len(files),
        }

        task = {
            "task_id":    tid,
            "sender":     self._my_id,
            "target":     target_id,
            "is_folder":  is_folder,
            "base_name":  base_name,
            "files":      files,
            "created_at": time.time(),
        }

        with self._lock:
            self._pending_offers[tid] = {
                "full_task": task,
                "offer":     offer,
                "target":    target_id,
            }

        self._send_cb(MSG_FILE_OFFER, target_id, json.dumps(offer).encode())
        self.status_changed.emit(f"等待对方确认: {display_name}...")
        log.log(TAG, f"_start_send: tid={tid}, {len(files)} files, {fmt_size(total_size)} → target={target_id}")

    def _launch_send_worker(self, tid: int) -> None:
        with self._lock:
            pkg = self._pending_offers.pop(tid, None)
        if not pkg:
            log.error(TAG, f"_launch_send_worker: task {tid} not found!")
            self.status_changed.emit("错误：找不到待发送任务")
            return

        task = pkg["full_task"]
        target = pkg["target"]
        display = task.get("base_name") or os.path.basename(task["files"][0]["abs"])

        worker = _SendWorker(
            task=task,
            send_cb=self._send_cb,
            lock=self._lock,
            on_progress=self.progress,
            on_status=self.status_changed,
            on_complete=lambda success: self._on_send_done(tid, success),
        )
        with self._lock:
            self._send_tasks[tid] = {"task": task, "worker": worker}

        self._store.save_send(task)

        self._send_task_meta_batches(task, target)
        log.log(TAG, f"_launch_send_worker: sent TASK_META (batched) tid={tid} → target={target}")

        self.status_changed.emit(f"开始传输: {display}...")

        thread = threading.Thread(target=worker.run, daemon=True, name=f"SendWorker-{tid}")
        thread.start()
        log.log(TAG, f"_launch_send_worker: worker started for tid={tid}")

    def _on_send_done(self, tid: int, success: bool = True) -> None:
        """发送完成回调。成功才删除任务；失败保留 .send_tasks 供断点续传。"""
        if success:
            with self._lock:
                self._send_tasks.pop(tid, None)
            self._store.delete_send(tid)
            log.log(TAG, f"_on_send_done: tid={tid} success, cleaned up")
        else:
            with self._lock:
                entry = self._send_tasks.get(tid)
                if entry:
                    entry["worker"] = None
            log.log(TAG, f"_on_send_done: tid={tid} incomplete, keep for resume")

    # ══════════════════════════════════════════
    # 元数据分片辅助方法
    # ══════════════════════════════════════════

    @staticmethod
    def _meta_file_entry(f: dict) -> dict:
        return {
            "rel": f["rel"],
            "size": f["size"],
            "fp": f["fp"],
            "chunk_count": f["chunk_count"],
        }

    @staticmethod
    def _resume_file_entry(f: dict) -> dict:
        """接收端→发送端的精简续传进度。大文件附带 received_seqs 位图。"""
        entry = {
            "fp": f["fp"],
            "recv": f.get("recv", 0),
            "acked_seq": f.get("acked_seq", -1),
        }
        if is_large_file(f):
            bs = f.get("received_seqs")
            if isinstance(bs, _BitSet):
                entry["received_seqs"] = bs.to_base64()
        return entry

    @staticmethod
    def _rebuild_file_entry(f: dict) -> dict:
        """接收端重建完整文件条目（补齐运行时字段）"""
        entry = {
            "rel": f["rel"],
            "size": f["size"],
            "fp": f["fp"],
            "chunk_count": f["chunk_count"],
            "recv": 0,
            "status": "pending",
            "acked_seq": -1,
        }
        if is_large_file(entry):
            entry["received_seqs"] = _BitSet(entry["chunk_count"])
            entry["data_complete"] = False
            entry["md5_verified"] = False
            entry["expected_md5"] = None
            entry["retransmit_round"] = 0
        return entry

    def _send_task_meta_batches(self, task: dict, target: int) -> None:
        files = task["files"]
        total = len(files)
        bsize = config.FILE_META_BATCH_SIZE
        batch_count = max(1, (total + bsize - 1) // bsize)

        for i in range(batch_count):
            batch = files[i * bsize:(i + 1) * bsize]
            payload = {
                "task_id": task["task_id"],
                "batch_idx": i,
                "batch_count": batch_count,
                "files": [self._meta_file_entry(f) for f in batch],
            }
            if i == 0:
                payload.update({
                    "sender": task["sender"],
                    "target": task["target"],
                    "is_folder": task["is_folder"],
                    "base_name": task["base_name"],
                    "created_at": task.get("created_at", time.time()),
                })
            self._send_cb(MSG_FILE_TASK_META, target, json.dumps(payload).encode())

    def _send_resume_req_batches(self, task: dict, target: int) -> None:
        files = task["files"]
        total = len(files)
        bsize = config.FILE_META_BATCH_SIZE
        batch_count = max(1, (total + bsize - 1) // bsize)

        for i in range(batch_count):
            batch = files[i * bsize:(i + 1) * bsize]
            payload = {
                "task_id": task["task_id"],
                "batch_idx": i,
                "batch_count": batch_count,
                "files": [self._resume_file_entry(f) for f in batch],
            }
            self._send_cb(MSG_FILE_RESUME_REQ, target, json.dumps(payload).encode())

    # ══════════════════════════════════════════
    # 接收端内部方法
    # ══════════════════════════════════════════

    def _on_recv_progress(self, task_id_str: str, pct: int, speed: str, eta: str) -> None:
        self.progress.emit(task_id_str, pct, speed, eta)

    def _on_recv_task_complete(self, tid: int) -> None:
        with self._lock:
            self._recv_tasks.pop(tid, None)
        self._store.delete_recv(tid)
        self.task_removed.emit(str(tid))

    def _save_recv_task(self, tid: int) -> None:
        """持久化接收任务进度（断点续传核心）。
        【bug修复】锁内 deepcopy 快照，锁外落盘，避免写盘/网络线程并发修改 task
        导致 _clean 遍历时读到不一致状态（尤其 received_seqs 位图）。"""
        with self._lock:
            task = self._recv_tasks.get(tid)
            if task:
                snapshot = copy.deepcopy(task)
            else:
                snapshot = None
        if snapshot:
            self._store.save_recv(snapshot)

    def _on_recv_checkpoint(self, tid: int) -> None:
        self._save_recv_task(tid)

    def _build_part_path(self, fi: dict, task: dict) -> str:
        base = task.get("base_name", "")
        rel = fi.get("rel", "")
        if not rel:
            rel = f"file_{fi.get('fp', 'unknown')}"
        if base:
            return os.path.join(self._base_dir, base, rel + ".part")
        return os.path.join(self._base_dir, rel + ".part")

    def _load_interrupted_tasks(self) -> None:
        tasks = self._store.load_recv_tasks()
        with self._lock:
            for t in tasks:
                tid = t["task_id"]
                self._recv_tasks[tid] = t
                display = t.get("base_name") or os.path.basename(t["files"][0].get("rel", ""))
                self.task_interrupted.emit(str(tid), display)
        if tasks:
            log.log(TAG, f"Loaded {len(tasks)} interrupted receive tasks")

    # ══════════════════════════════════════════
    # 消息处理器
    # ══════════════════════════════════════════

    def _handle_offer(self, sender_id: int, payload: bytes) -> None:
        try:
            offer = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warn(TAG, f"Invalid file offer JSON: {e}")
            return

        tid = offer.get("task_id")
        if tid is None:
            log.warn(TAG, "File offer missing task_id")
            return

        with self._lock:
            # 【bug修复】限制邀约缓冲大小，防止异常情况下无限增长
            if len(self._received_offers) >= 100:
                # 移除最旧的一个（dict 插入序）
                oldest = next(iter(self._received_offers))
                self._received_offers.pop(oldest, None)
            self._received_offers[tid] = {"offer": offer, "sid": sender_id}

        display = offer.get("display_name", "未知")
        size_desc = offer.get("size_desc", "")
        self.file_offer_received.emit(str(tid), display, size_desc)

    def _handle_offer_resp(self, sender_id: int, payload: bytes) -> None:
        if len(payload) < 2:
            return
        accepted = (payload[0] == 0x01)
        try:
            resp = json.loads(payload[1:].decode())
            tid = resp.get("task_id")
            if tid is None:
                return
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warn(TAG, f"_handle_offer_resp: JSON decode failed: {e}")
            return

        if not accepted:
            with self._lock:
                self._pending_offers.pop(tid, None)
            self.status_changed.emit("对方拒绝了传输")
            return

        self._launch_send_worker(tid)

    def _handle_task_meta(self, sender_id: int, payload: bytes) -> None:
        try:
            meta = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warn(TAG, f"_handle_task_meta: JSON decode failed: {e}")
            return

        tid = meta.get("task_id")
        if tid is None:
            return

        batch_idx = meta.get("batch_idx", 0)
        batch_count = meta.get("batch_count", 1)
        batch_files = meta.get("files", [])

        assembled = None
        with self._lock:
            buf = self._meta_buffers.get(tid)
            if buf is None:
                buf = {"batches": {}, "batch_count": batch_count, "base": {}}
                self._meta_buffers[tid] = buf

            if batch_idx == 0:
                buf["base"] = {
                    "sender": meta.get("sender", self._my_id),
                    "target": meta.get("target", sender_id),
                    "is_folder": meta.get("is_folder", False),
                    "base_name": meta.get("base_name", ""),
                    "created_at": meta.get("created_at", time.time()),
                }
            if batch_idx not in buf["batches"]:
                buf["batches"][batch_idx] = batch_files

            if len(buf["batches"]) >= buf["batch_count"]:
                task = dict(buf["base"])
                task["task_id"] = tid
                task["origin_sender"] = sender_id

                full_meta = []
                for i in range(buf["batch_count"]):
                    full_meta.extend(buf["batches"].get(i, []))
                task["files"] = [self._rebuild_file_entry(f) for f in full_meta]

                # 续传恢复：从旧任务继承大文件 received_seqs
                old = self._recv_tasks.get(tid)
                if old:
                    old_map = {f["fp"]: f for f in old["files"]}
                    for rf in task["files"]:
                        old_fi = old_map.get(rf["fp"])
                        if old_fi is not None:
                            if is_large_file(rf):
                                old_bs = old_fi.get("received_seqs")
                                rf["received_seqs"] = old_bs if isinstance(old_bs, _BitSet) else _BitSet(rf["chunk_count"])
                                rf["recv"] = min(rf["received_seqs"].count() * config.FILE_CHUNK_SIZE, rf["size"])
                            else:
                                # 【bug修复】小文件续传：已完成文件 .part 已 rename 走，
                                # 需检查正式文件是否存在且大小正确，避免误判为未接收而重发。
                                part_path = self._build_part_path(rf, task)
                                final_path = part_path[:-5] if part_path.endswith(".part") else part_path
                                if os.path.exists(final_path) and os.path.getsize(final_path) == rf["size"]:
                                    rf["recv"] = rf["size"]
                                    rf["status"] = "completed"
                                else:
                                    rf["recv"] = os.path.getsize(part_path) if os.path.exists(part_path) else 0

                self._recv_tasks[tid] = task
                del self._meta_buffers[tid]
                assembled = task

        if assembled is None:
            return

        self._save_recv_task(tid)
        display = assembled.get("base_name") or os.path.basename(assembled["files"][0].get("rel", "unknown"))
        total = fmt_size(sum(f["size"] for f in assembled["files"]))
        self.status_changed.emit(f"正在接收: {display} ({total})")
        log.log(TAG, f"_handle_task_meta: assembled tid={tid}, {len(assembled['files'])} files ({total})")

        self._send_resume_req_batches(assembled, sender_id)

    def _apply_progress_to_task(self, task: dict, all_progress: list) -> None:
        """把续传进度应用到发送任务。大文件从 received_seqs 位图计算缺失 seq。"""
        local_map = {f["fp"]: f for f in task["files"]}
        for p in all_progress:
            fp = p.get("fp")
            if fp not in local_map:
                continue
            fi = local_map[fp]
            fi["recv"] = p.get("recv", 0)
            fi["acked_seq"] = p.get("acked_seq", -1)
            if is_large_file(fi) and "received_seqs" in p:
                bs = _BitSet.from_base64(fi.get("chunk_count", 0), p["received_seqs"])
                fi["received_seqs"] = bs
                # 【bug修复】仅当存在已接收进度时才走"补发缺失 seq"路径。
                # 全新任务位图全空（count==0），应走正常顺序发送 + 流式 MD5，
                # 否则会误走补发路径，多一次全量 MD5 读盘。
                if bs.count() > 0:
                    missing = bs.missing()
                    if missing:
                        fi["_missing_seqs"] = missing
                    else:
                        fi["_missing_seqs"] = None
                        fi["status"] = "completed"  # 已完整，无需重发
            else:
                # 【bug修复】小文件续传：仅当 size > 0 且 recv >= size 时标记 completed。
                # 空文件（size=0）必须正常发送 EOF，不能标记 completed，
                # 否则发送端会跳过，接收端永远收不到空文件。
                if not is_large_file(fi) and fi.get("size", 0) > 0 and fi.get("recv", 0) >= fi.get("size", 0):
                    fi["status"] = "completed"

    def _handle_resume_req(self, sender_id: int, payload: bytes) -> None:
        try:
            meta = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warn(TAG, f"_handle_resume_req: JSON decode failed: {e}")
            return

        tid = meta.get("task_id")
        if tid is None:
            return

        batch_idx = meta.get("batch_idx", 0)
        batch_count = meta.get("batch_count", 1)
        batch_files = meta.get("files", [])

        # 锁内只做缓冲累积和判断，耗时操作（磁盘 IO / 启动线程）移到锁外
        action = None
        all_progress = None
        entry = None
        with self._lock:
            buf = self._resume_buffers.get(tid)
            if buf is None:
                buf = {"batches": {}, "batch_count": batch_count}
                self._resume_buffers[tid] = buf
            if batch_idx not in buf["batches"]:
                buf["batches"][batch_idx] = batch_files

            if len(buf["batches"]) >= buf["batch_count"]:
                all_progress = []
                for i in range(buf["batch_count"]):
                    all_progress.extend(buf["batches"].get(i, []))
                del self._resume_buffers[tid]

                entry = self._send_tasks.get(tid)
                if entry is not None:
                    task = entry["task"]
                    self._apply_progress_to_task(task, all_progress)
                    worker = entry.get("worker")
                    action = "signal" if worker is not None else "rebuild"
                else:
                    action = "restore"

        # 锁外执行耗时操作
        if action == "signal":
            with self._lock:
                entry = self._send_tasks.get(tid)
                worker = entry.get("worker") if entry else None
            if worker is not None:
                worker.signal_resume_ready()
        elif action == "rebuild":
            with self._lock:
                entry = self._send_tasks.get(tid)
                task = entry["task"] if entry else None
            if task is not None:
                self._rebuild_send_worker(tid, task)
        elif action == "restore":
            self._restore_send_worker_from_disk(tid, sender_id, all_progress)

    def _rebuild_send_worker(self, tid: int, task: dict) -> None:
        """为内存中已存在的任务重建 worker 并启动（续传）"""
        worker = _SendWorker(
            task=task,
            send_cb=self._send_cb,
            lock=self._lock,
            on_progress=self.progress,
            on_status=self.status_changed,
            on_complete=lambda success: self._on_send_done(tid, success),
        )
        # 续传进度已应用，无需再等 RESUME_REQ
        worker.signal_resume_ready()
        with self._lock:
            entry = self._send_tasks.get(tid)
            if entry is None:
                self._send_tasks[tid] = {"task": task, "worker": worker}
            else:
                entry["worker"] = worker
        self._store.save_send(task)
        display = task.get("base_name") or os.path.basename(task["files"][0].get("abs", "unknown"))
        self.status_changed.emit(f"恢复续传: {display}...")
        log.log(TAG, f"_rebuild_send_worker: rebuilt tid={tid}")
        thread = threading.Thread(target=worker.run, daemon=True, name=f"SendWorker-{tid}")
        thread.start()

    def _restore_send_worker_from_disk(self, tid: int, sender_id: int, progress: list) -> None:
        """【断点续传】从 .send_tasks 恢复发送任务并重建 worker，应用续传进度"""
        task = None
        for t in self._store.load_send_tasks():
            if t.get("task_id") == tid:
                task = t
                break
        if task is None:
            log.warn(TAG, f"_restore_send_worker: task {tid} not found on disk")
            return

        task["target"] = task.get("target", sender_id)
        for f in task.get("files", []):
            f.setdefault("recv", 0)
            f.setdefault("acked_seq", -1)
            f.setdefault("status", "pending")

        self._apply_progress_to_task(task, progress)

        self._rebuild_send_worker(tid, task)
        log.log(TAG, f"_restore_send_worker: restored tid={tid}, {len(task['files'])} files")

    def _handle_resume_ack(self, sender_id: int, payload: bytes) -> None:
        pass

    def _handle_chunk(self, sender_id: int, payload: bytes) -> None:
        if len(payload) < CHUNK_HEADER.size:
            return

        tid, idx, seq = CHUNK_HEADER.unpack(payload[:CHUNK_HEADER.size])
        data = payload[CHUNK_HEADER.size:]

        with self._lock:
            task = self._recv_tasks.get(tid)
            if not task:
                return
            if idx >= len(task["files"]):
                return
            fi = task["files"][idx]

            is_eof = (seq == EOF_SEQ)

            if is_eof:
                fi["eof_received"] = True
            # 不做 seq 去重：大文件随机写幂等，重复写无副作用；去重会破坏重传

            part_path = self._build_part_path(fi, task)

        self._recv_worker.enqueue((tid, idx, part_path, seq, data if not is_eof else b"", fi, task))

    def _handle_ack(self, sender_id: int, payload: bytes) -> None:
        if len(payload) < ACK_STRUCT.size:
            return
        tid, idx, acked_seq = ACK_STRUCT.unpack(payload)
        with self._lock:
            entry = self._send_tasks.get(tid)
            if entry:
                worker = entry.get("worker")
                if worker:
                    worker.on_ack(idx, acked_seq)

    def _handle_retransmit_req(self, sender_id: int, payload: bytes) -> None:
        try:
            req = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warn(TAG, f"_handle_retransmit_req: JSON decode failed: {e}")
            return

        tid = req.get("task_id")
        file_idx = req.get("file_idx")
        seqs = req.get("seqs", [])
        if tid is None or file_idx is None:
            return

        with self._lock:
            entry = self._send_tasks.get(tid)
            if entry:
                worker = entry.get("worker")
                if worker:
                    worker.on_retransmit_req(file_idx, seqs)
                    log.log(TAG, f"_handle_retransmit_req: {len(seqs)} chunks for file {file_idx}")

    def _handle_verify(self, sender_id: int, payload: bytes) -> None:
        try:
            v = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warn(TAG, f"_handle_verify: JSON decode failed: {e}")
            return

        tid = v.get("task_id")
        file_idx = v.get("file_idx")
        md5 = v.get("md5", "")
        if tid is None or file_idx is None:
            return

        with self._lock:
            task = self._recv_tasks.get(tid)
            if not task or file_idx >= len(task["files"]):
                return
            fi = task["files"][file_idx]
            fi["expected_md5"] = md5
            part_path = self._build_part_path(fi, task)

        self._recv_worker.enqueue((tid, file_idx, part_path, EOF_SEQ, b"", fi, task))

    def _handle_cancel(self, sender_id: int, payload: bytes) -> None:
        if len(payload) < CANCEL_FMT.size:
            return
        tid = CANCEL_FMT.unpack(payload[:CANCEL_FMT.size])[0]

        with self._lock:
            send_entry = self._send_tasks.get(tid)
            if send_entry:
                worker = send_entry.get("worker")
                if worker:
                    worker.cancel()
                self._send_tasks.pop(tid, None)
                self._store.delete_send(tid)
                self.status_changed.emit("对方取消了接收")
                return

        self.clear_task(str(tid))
        self.status_changed.emit("对方取消了发送")
