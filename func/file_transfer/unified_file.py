# -*- coding: utf-8 -*-
"""
统一文件传输管理器 (P2P 通用)
彻底消除房主/房客差异，支持任意节点互传、本地回环、断点续传。
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
    HOST_ID
)
import config

TAG = "UnifiedFile"

class UnifiedFileTransfer(QObject):
    progress = Signal(str, int, str, str)          # task_id, percent, speed, eta
    file_complete = Signal(str, str)               # task_id, final_path
    status_changed = Signal(str)
    task_interrupted = Signal(str, str)            # task_id, display_name
    task_removed = Signal(str)

    def __init__(self, my_id: int, send_callback: Callable):
        super().__init__()
        self._my_id = my_id
        self._send_callback = send_callback  # fn(msg_type, target_id, payload)
        
        self._save_dir = config.DOWNLOAD_DIR
        os.makedirs(self._save_dir, exist_ok=True)
        self._resume_dir = os.path.join(self._save_dir, ".resume_tasks")
        os.makedirs(self._resume_dir, exist_ok=True)

        self._recv_tasks = {}
        self._send_tasks = {}
        self._lock = threading.Lock()
        
        self._write_queue = queue.Queue()
        self._write_running = True
        threading.Thread(target=self._write_loop, daemon=True, name="FileWriter").start()
        
        self._load_interrupted_tasks()

    # ═════════════════════════════════════════
    # 网络层入口 (由 MainWindow 调用)
    # ═════════════════════════════════════════
    def handle_incoming(self, msg_type: int, sender_id: int, payload: bytes):
        if msg_type == MSG_FILE_TASK_META: self._on_task_meta(sender_id, payload)
        elif msg_type == MSG_FILE_RESUME_REQ: self._on_resume_req(sender_id, payload)
        elif msg_type == MSG_FILE_RESUME_ACK: self._on_resume_ack(sender_id, payload)
        elif msg_type == MSG_FILE_CHUNK: self._on_chunk(sender_id, payload)

    # ═════════════════════════════════════════
    # 发送方逻辑
    # ═════════════════════════════════════════
    def send_file(self, file_path: str, target_id: int):
        self._start_send([file_path], target_id, False)

    def send_folder(self, folder_path: str, target_id: int):
        files = [os.path.join(r, f) for r, _, fs in os.walk(folder_path) for f in fs]
        self._start_send(files, target_id, True, os.path.basename(folder_path), folder_path)

    def _start_send(self, abs_paths, target_id, is_folder, base_name="", root_path=""):
        task_id = int(time.time() * 1000) & 0xFFFFFFFF
        files = []
        for p in abs_paths:
            size = os.path.getsize(p)
            fp = hashlib.md5(f"{size}_{os.path.getmtime(p)}".encode()).hexdigest()[:8]
            rel = os.path.relpath(p, root_path) if root_path else os.path.basename(p)
            files.append({"abs": p.replace("\\", "/"), "rel": rel.replace("\\", "/"), 
                          "size": size, "fp": fp, "recv": 0, "status": "pending"})
        
        task = {"task_id": task_id, "sender": self._my_id, "target": target_id, 
                "is_folder": is_folder, "base_name": base_name, "files": files}
        
        with self._lock:
            self._send_tasks[task_id] = {"task": task, "event": threading.Event(), "cancelled": False}
        
        meta = json.dumps(task).encode("utf-8")
        self._send_callback(MSG_FILE_TASK_META, target_id, meta)
        self.status_changed.emit(f"等待 {target_id} 接收...")
        threading.Thread(target=self._wait_and_send, args=(task_id,), daemon=True).start()

    def _wait_and_send(self, task_id: int):
        with self._lock: state = self._send_tasks.get(task_id)
        if not state: return
        
        if state["event"].wait(timeout=60):
            if state["cancelled"]: return
            self._send_callback(MSG_FILE_RESUME_ACK, state["task"]["target"], b"\x00")
            self._do_send_chunks(task_id)
        else:
            log.error(TAG, f"Send timeout: {task_id}")
            self.status_changed.emit("发送超时")

    def _do_send_chunks(self, task_id: int):
        with self._lock: state = self._send_tasks.get(task_id)
        if not state: return
        
        task = state["task"]
        total_size = sum(f["size"] for f in task["files"])
        sent = sum(f["recv"] for f in task["files"])
        start_time = time.time()
        
        for idx, f_info in enumerate(task["files"]):
            if f_info["status"] == "completed": continue
            try:
                with open(f_info["abs"], "rb") as f:
                    f.seek(f_info["recv"])
                    while not state["cancelled"]:
                        chunk = f.read(config.FILE_CHUNK_SIZE)
                        if not chunk: break
                        header = struct.pack("!II", task_id, idx)
                        self._send_callback(MSG_FILE_CHUNK, task["target"], header + chunk)
                        sent += len(chunk)
                        
                        now = time.time()
                        speed = sent / (now - start_time) if now > start_time else 0
                        self.progress.emit(str(task_id), int(sent*100/total_size), self._fmt_speed(speed), "")
            except Exception as e:
                log.error(TAG, f"Read file error: {e}")
                break
        
        self.status_changed.emit("发送完成")

    def _on_resume_req(self, sender_id: int, payload: bytes):
        try:
            remote = json.loads(payload.decode("utf-8"))
            task_id = remote["task_id"]
            with self._lock: state = self._send_tasks.get(task_id)
            if state:
                # 更新断点
                local_map = {f["fp"]: f for f in state["task"]["files"]}
                for rf in remote["files"]:
                    if rf["fp"] in local_map: local_map[rf["fp"]]["recv"] = rf["recv"]
                state["event"].set()
        except Exception as e:
            log.error(TAG, f"Resume req error: {e}")

    # ═════════════════════════════════════════
    # 接收方逻辑
    # ═════════════════════════════════════════
    def _on_task_meta(self, sender_id: int, payload: bytes):
        try:
            task = json.loads(payload.decode("utf-8"))
        except: return
        
        task_id = task["task_id"]
        task["origin_sender"] = sender_id
        
        with self._lock:
            local = self._recv_tasks.get(task_id)
            if local:
                local_map = {f["fp"]: f for f in local["files"]}
                for rf in task["files"]:
                    if rf["fp"] in local_map:
                        part = self._part_path(rf, task)
                        if os.path.exists(part) and os.path.getsize(part) == local_map[rf["fp"]]["recv"]:
                            rf["recv"] = local_map[rf["fp"]]["recv"]
        
        with self._lock: self._recv_tasks[task_id] = task
        self._save_json(task_id)
        
        display = task.get("base_name") or os.path.basename(task["files"][0]["abs"])
        self.status_changed.emit(f"正在接收: {display}")
        
        req = json.dumps(task).encode("utf-8")
        self._send_callback(MSG_FILE_RESUME_REQ, sender_id, req)

    def _on_resume_ack(self, sender_id: int, payload: bytes):
        pass # 接收方不需要处理 ACK

    def _on_chunk(self, sender_id: int, payload: bytes):
        if len(payload) < 8: return
        task_id, idx = struct.unpack("!II", payload[:8])
        data = payload[8:]
        
        with self._lock:
            task = self._recv_tasks.get(task_id)
            if not task or idx >= len(task["files"]): return
            
        part = self._part_path(task["files"][idx], task)
        self._write_queue.put((task_id, idx, part, data))

    def _write_loop(self):
        last_report = {}
        last_bytes = {}
        while self._write_running:
            try:
                task_id, idx, part, data = self._write_queue.get(timeout=0.5)
            except queue.Empty: continue
            
            with self._lock:
                task = self._recv_tasks.get(task_id)
                if not task: continue
                f_info = task["files"][idx]
                
            try:
                os.makedirs(os.path.dirname(part) or ".", exist_ok=True)
                with open(part, "ab") as f: f.write(data)
                f_info["recv"] += len(data)
                
                if f_info["recv"] >= f_info["size"]:
                    f_info["status"] = "completed"
                    final = self._final_path(f_info, task)
                    os.makedirs(os.path.dirname(final) or ".", exist_ok=True)
                    if os.path.exists(final): os.remove(final)
                    os.rename(part, final)
                    self.file_complete.emit(str(task_id), final)
                
                self._save_json(task_id)
                
                now = time.time()
                total = sum(f["size"] for f in task["files"])
                recv = sum(f["recv"] for f in task["files"])
                
                if task_id not in last_report: last_report[task_id] = now; last_bytes[task_id] = recv
                elif now - last_report[task_id] >= 1.0:
                    speed = (recv - last_bytes[task_id]) / (now - last_report[task_id])
                    self.progress.emit(str(task_id), int(recv*100/total), self._fmt_speed(speed), "")
                    last_report[task_id] = now; last_bytes[task_id] = recv
                    
                if all(f["status"] == "completed" for f in task["files"]):
                    self._remove_json(task_id)
                    with self._lock: self._recv_tasks.pop(task_id, None)
                    self.status_changed.emit("接收完成")
            except Exception as e:
                log.error(TAG, f"Write error: {e}")

    # ═════════════════════════════════════════
    # 断点续传与清理
    # ═════════════════════════════════════════
    def resume_task(self, task_id: str):
        with self._lock: task = self._recv_tasks.get(int(task_id))
        if task and "origin_sender" in task:
            self._send_callback(MSG_FILE_RESUME_REQ, task["origin_sender"], json.dumps(task).encode("utf-8"))

    def clear_task(self, task_id: str):
        with self._lock: task = self._recv_tasks.pop(int(task_id), None)
        if task:
            for f in task["files"]:
                part = self._part_path(f, task)
                try:
                    if os.path.exists(part): 
                        # 【修复】Windows 防崩溃：先重命名再删除
                        trash = part + ".trash"
                        os.rename(part, trash)
                        os.remove(trash)
                except Exception as e:
                    log.warn(TAG, f"Clear file failed: {e}")
            self._remove_json(int(task_id))

    def _load_interrupted_tasks(self):
        for f in os.listdir(self._resume_dir):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(self._resume_dir, f), "r", encoding="utf-8") as fp:
                        task = json.load(fp)
                        with self._lock: self._recv_tasks[task["task_id"]] = task
                        display = task.get("base_name") or os.path.basename(task["files"][0]["abs"])
                        self.task_interrupted.emit(str(task["task_id"]), display)
                except: pass

    def _save_json(self, task_id):
        with self._lock: task = self._recv_tasks.get(task_id)
        if task:
            with open(os.path.join(self._resume_dir, f"{task_id}.json"), "w", encoding="utf-8") as f:
                json.dump(task, f)

    def _remove_json(self, task_id):
        path = os.path.join(self._resume_dir, f"{task_id}.json")
        if os.path.exists(path): os.remove(path)
        self.task_removed.emit(str(task_id))

    def _part_path(self, f_info, task):
        base = task.get("base_name")
        return os.path.join(self._save_dir, base, f_info["rel"] + ".part") if base else os.path.join(self._save_dir, f_info["rel"] + ".part")

    def _final_path(self, f_info, task):
        base = task.get("base_name")
        return os.path.join(self._save_dir, base, f_info["rel"]) if base else os.path.join(self._save_dir, f_info["rel"])

    @staticmethod
    def _fmt_speed(bps: float) -> str:
        if bps >= 1048576: return f"{bps/1048576:.1f} MB/s"
        if bps >= 1024: return f"{bps/1024:.1f} KB/s"
        return f"{bps:.0f} B/s"

    def cleanup(self):
        self._write_running = False
