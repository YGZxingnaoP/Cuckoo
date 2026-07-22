# -*- coding: utf-8 -*-
"""
文件传输 —— 房客端 (接收 & 发送)
"""

import os
import json
import hashlib
import struct
import threading
import time
import queue
from typing import Optional

from PySide6.QtCore import QObject, Signal

from common import logger as log
from core.protocol import (
    MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK, MSG_FILE_CHUNK,
    build_frame
)
import config

TAG = "GuestFile"

class GuestFileReceiver(QObject):
    """房客文件接收器 (基于 JSON 状态机)"""
    progress = Signal(str, int, str, str)
    file_complete = Signal(str, str)
    status_changed = Signal(str)
    task_interrupted = Signal(str, str)
    task_removed = Signal(str)

    def __init__(self, save_dir: str = "", client_conn=None):
        super().__init__()
        self._save_dir = save_dir or config.DOWNLOAD_DIR
        self._client_conn = client_conn
        self._resume_dir = os.path.join(self._save_dir, ".resume_tasks")
        os.makedirs(self._resume_dir, exist_ok=True)
        
        self._tasks = {}
        self._lock = threading.Lock()
        
        self._write_queue = queue.Queue()
        self._write_running = True
        self._write_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._write_thread.start()
        
        self._load_interrupted_tasks()

    def _load_interrupted_tasks(self):
        for f in os.listdir(self._resume_dir):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(self._resume_dir, f), "r", encoding="utf-8") as fp:
                        task = json.load(fp)
                        with self._lock:
                            self._tasks[task["task_id"]] = task
                        display = task.get("base_name", "") or os.path.basename(task["files"][0]["abs_path"])
                        self.task_interrupted.emit(str(task["task_id"]), display)
                except Exception as e:
                    log.error(TAG, f"Load resume task error: {e}")

    def _save_task(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task: return
            path = os.path.join(self._resume_dir, f"{task_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False)

    def _remove_task(self, task_id):
        with self._lock:
            self._tasks.pop(task_id, None)
        path = os.path.join(self._resume_dir, f"{task_id}.json")
        if os.path.exists(path): os.remove(path)
        self.task_removed.emit(str(task_id))

    def handle_task_meta(self, sender_id: int, payload: bytes):
        try:
            task = json.loads(payload.decode("utf-8"))
        except: return
        
        task_id = task["task_id"]
        task["origin_sender_id"] = sender_id  # 记录原始发送者，用于断点续传
        
        with self._lock:
            local_task = self._tasks.get(task_id)
            
        if local_task:
            local_map = {f["fingerprint"]: f for f in local_task["files"]}
            for remote_f in task["files"]:
                if remote_f["fingerprint"] in local_map:
                    local_f = local_map[remote_f["fingerprint"]]
                    part_path = self._get_part_path(remote_f, task)
                    if os.path.exists(part_path) and os.path.getsize(part_path) == local_f["received_bytes"]:
                        remote_f["received_bytes"] = local_f["received_bytes"]
                        remote_f["status"] = "transferring" if remote_f["received_bytes"] < remote_f["size"] else "completed"
                    else:
                        remote_f["received_bytes"] = 0
                        remote_f["status"] = "pending"
                else:
                    remote_f["received_bytes"] = 0
                    remote_f["status"] = "pending"
        
        with self._lock:
            self._tasks[task_id] = task
        self._save_task(task_id)
        
        req_payload = json.dumps(task).encode("utf-8")
        self._client_conn.send_frame(MSG_FILE_RESUME_REQ, sender_id, req_payload)
        
        display = task.get("base_name", "") or os.path.basename(task["files"][0]["abs_path"])
        self.status_changed.emit(f"正在接收: {display}")
        self.task_interrupted.emit(str(task_id), display)

    def handle_file_chunk(self, sender_id: int, payload: bytes):
        if len(payload) < 8: return
        task_id, file_index = struct.unpack("!II", payload[:8])  # 修复溢出 Bug
        data = payload[8:]
        
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or file_index >= len(task["files"]): return
            file_info = task["files"][file_index]
            if file_info["status"] == "completed": return
            
        part_path = self._get_part_path(file_info, task)
        self._write_queue.put((task_id, file_index, part_path, data))

    def _write_loop(self):
        last_report = {}
        last_bytes = {}
        while self._write_running:
            try:
                task_id, file_index, part_path, data = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            
            with self._lock:
                task = self._tasks.get(task_id)
                if not task: continue
                file_info = task["files"][file_index]
                
            try:
                dir_name = os.path.dirname(part_path)
                if dir_name: os.makedirs(dir_name, exist_ok=True)  # 修复空目录 Bug
                
                with open(part_path, "ab") as f:
                    f.write(data)
                
                file_info["received_bytes"] += len(data)
                
                if file_info["received_bytes"] >= file_info["size"]:
                    file_info["status"] = "completed"
                    final_path = self._get_final_path(file_info, task)
                    if os.path.dirname(final_path): os.makedirs(os.path.dirname(final_path), exist_ok=True)
                    if os.path.exists(final_path): os.remove(final_path)
                    os.rename(part_path, final_path)
                    self.file_complete.emit(str(task_id), final_path)
                
                self._save_task(task_id)
                
                now = time.time()
                total_size = sum(f["size"] for f in task["files"])
                total_recv = sum(f["received_bytes"] for f in task["files"])
                
                if task_id not in last_report:
                    last_report[task_id] = now
                    last_bytes[task_id] = total_recv
                else:
                    if now - last_report[task_id] >= 1.0:  # 修复首次速度计算 Bug
                        percent = int(total_recv * 100 / total_size) if total_size > 0 else 100
                        last_b = last_bytes[task_id]
                        speed = (total_recv - last_b) / (now - last_report[task_id])
                        speed_str = self._format_speed(speed)
                        eta = (total_size - total_recv) / speed if speed > 0 else 0
                        
                        self.progress.emit(str(task_id), percent, speed_str, self._format_eta(eta))
                        last_report[task_id] = now
                        last_bytes[task_id] = total_recv
                
                if all(f["status"] == "completed" for f in task["files"]):
                    self._remove_task(task_id)
                    self.status_changed.emit("接收完成")
                    
            except Exception as e:
                log.error(TAG, f"Write error: {e}")

    def resume_task(self, task_id: str):
        with self._lock:
            task = self._tasks.get(int(task_id))
        if task and "origin_sender_id" in task:
            req_payload = json.dumps(task).encode("utf-8")
            self._client_conn.send_frame(MSG_FILE_RESUME_REQ, task["origin_sender_id"], req_payload)

    def clear_task(self, task_id: str):
        with self._lock:
            task = self._tasks.pop(int(task_id), None)
        if task:
            for f in task["files"]:
                part_path = self._get_part_path(f, task)
                if os.path.exists(part_path): os.remove(part_path)
            self._remove_task(int(task_id))

    def _get_part_path(self, file_info, task):
        if task.get("base_name"):
            return os.path.join(self._save_dir, task["base_name"], file_info["rel_path"] + ".part")
        return os.path.join(self._save_dir, file_info["rel_path"] + ".part")

    def _get_final_path(self, file_info, task):
        if task.get("base_name"):
            return os.path.join(self._save_dir, task["base_name"], file_info["rel_path"])
        return os.path.join(self._save_dir, file_info["rel_path"])

    @staticmethod
    def _format_speed(bps: float) -> str:
        if bps >= 1024 * 1024: return f"{bps / (1024 * 1024):.1f} MB/s"
        elif bps >= 1024: return f"{bps / 1024:.1f} KB/s"
        return f"{bps:.0f} B/s"

    @staticmethod
    def _format_eta(seconds: float) -> str:
        if seconds <= 0: return "即将完成"
        h, m, s = int(seconds // 3600), int((seconds % 3600) // 60), int(seconds % 60)
        if h > 0: return f"{h}h{m}m{s}s"
        elif m > 0: return f"{m}m{s}s"
        return f"{s}s"

    def cleanup(self):
        self._write_running = False
        self._write_thread.join(timeout=2)


class GuestFileSender:
    """房客作为发送方 (支持动态重建)"""
    def __init__(self, client_conn, task_dict=None):
        self._client_conn = client_conn
        self._resume_event = threading.Event()
        
        if task_dict:
            self._current_task = task_dict
            self.task_id = task_dict["task_id"]
            self._target_id = task_dict["target_id"]
        else:
            self._current_task = None
            self.task_id = 0
            self._target_id = 0

    @classmethod
    def from_json(cls, client_conn, task_dict):
        return cls(client_conn, task_dict)

    def send_file(self, file_path: str, target_id: int, sender_id: int):
        task = self._create_task([file_path], target_id, sender_id, False)
        self._start_send(task, target_id)

    def send_folder(self, folder_path: str, target_id: int, sender_id: int):
        file_list = []
        for root, _, files in os.walk(folder_path):
            for f in files:
                file_list.append(os.path.join(root, f))
        task = self._create_task(file_list, target_id, sender_id, True, os.path.basename(folder_path), folder_path)
        self._start_send(task, target_id)

    def _create_task(self, abs_paths, target_id, sender_id, is_folder, base_name="", root_path=""):
        task_id = int(time.time() * 1000) & 0xFFFFFFFF
        files = []
        for p in abs_paths:
            size = os.path.getsize(p)
            mtime = os.path.getmtime(p)
            fp = hashlib.md5(f"{size}_{mtime}".encode()).hexdigest()[:8]
            rel = os.path.relpath(p, root_path) if root_path else os.path.basename(p)
            files.append({
                "abs_path": p.replace("\\", "/"),
                "rel_path": rel.replace("\\", "/"),
                "size": size,
                "fingerprint": fp,
                "received_bytes": 0,
                "status": "pending"
            })
        return {"task_id": task_id, "sender_id": sender_id, "target_id": target_id, "is_folder": is_folder, "base_name": base_name, "files": files}

    def _start_send(self, task, target_id):
        self._current_task = task
        self.task_id = task["task_id"]
        self._target_id = target_id
        self._resume_event.clear()
        meta_payload = json.dumps(task).encode("utf-8")
        self._client_conn.send_frame(MSG_FILE_TASK_META, target_id, meta_payload)
        threading.Thread(target=self._wait_and_send, args=(target_id,), daemon=True).start()

    def _wait_and_send(self, target_id):
        if self._resume_event.wait(timeout=60):
            self._client_conn.send_frame(MSG_FILE_RESUME_ACK, target_id, b"\x00")
            self._send_chunks(target_id)

    def handle_resume_req(self, payload):
        try:
            remote_task = json.loads(payload.decode("utf-8"))
            if not self._current_task or self._current_task["task_id"] != remote_task["task_id"]:
                # 动态重建发送上下文 (解决发送方断线重连后的续传问题)
                self._current_task = remote_task
                self.task_id = remote_task["task_id"]
                self._target_id = remote_task["target_id"]
                self._resume_event.set()
                return

            for rf in remote_task["files"]:
                for lf in self._current_task["files"]:
                    if lf["abs_path"] == rf["abs_path"] and lf["fingerprint"] == rf["fingerprint"]:
                        lf["received_bytes"] = rf["received_bytes"]
                        break
            self._resume_event.set()
        except Exception as e:
            log.error(TAG, f"Parse resume req error: {e}")

    def _send_chunks(self, target_id):
        for idx, file_info in enumerate(self._current_task["files"]):
            if file_info["status"] == "completed": continue
            try:
                with open(file_info["abs_path"], "rb") as f:
                    f.seek(file_info["received_bytes"])
                    while True:
                        chunk = f.read(config.FILE_CHUNK_SIZE)
                        if not chunk: break
                        header = struct.pack("!II", self._current_task["task_id"], idx)  # 修复溢出
                        self._client_conn.send_frame(MSG_FILE_CHUNK, target_id, header + chunk)
            except Exception as e:
                log.error(TAG, f"Send chunk error: {e}")
                break
