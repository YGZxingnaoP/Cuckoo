# -*- coding: utf-8 -*-
"""
文件传输 —— 主机端处理器 (发送 & 透明中转)
"""

import os
import json
import hashlib
import struct
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

from common import logger as log
from core.server import Server
from core.protocol import (
    build_frame, MSG_FILE_TASK_META, MSG_FILE_RESUME_REQ, MSG_FILE_RESUME_ACK, MSG_FILE_CHUNK,
    HOST_ID, BROADCAST_ID
)
import config

TAG = "HostFile"

class HostFileHandler(QObject):
    progress = Signal(str, int, str, str)
    file_complete = Signal(str, str)
    status_changed = Signal(str)

    def __init__(self, server: Server):
        super().__init__()
        self._server = server
        self._senders = {}
        self._lock = threading.Lock()

        server.register_handler(MSG_FILE_TASK_META, self._handle_task_meta)
        server.register_handler(MSG_FILE_RESUME_REQ, self._handle_resume_req)
        server.register_handler(MSG_FILE_RESUME_ACK, self._handle_resume_ack)
        server.register_handler(MSG_FILE_CHUNK, self._handle_chunk)

    def get_sender(self, task_id):
        with self._lock: return self._senders.get(task_id)

    def register_sender(self, sender):
        with self._lock: self._senders[sender.task_id] = sender

    def _handle_task_meta(self, msg_type, sender_id, target_id, payload):
        if target_id == HOST_ID: return
        relay = build_frame(MSG_FILE_TASK_META, sender_id, target_id, payload)
        self._server.send_to(target_id, relay, MSG_FILE_TASK_META)

    def _handle_resume_req(self, msg_type, sender_id, target_id, payload):
        if target_id == HOST_ID:
            try:
                task = json.loads(payload.decode("utf-8"))
                sender = self.get_sender(task["task_id"])
                if not sender:
                    sender = HostFileSender.from_json(self._server, task)
                    self.register_sender(sender)
                sender.handle_resume_req(payload)
            except Exception as e:
                log.error(TAG, f"Host parse resume req error: {e}")
            return
        
        relay = build_frame(MSG_FILE_RESUME_REQ, sender_id, target_id, payload)
        self._server.send_to(target_id, relay, MSG_FILE_RESUME_REQ)

    def _handle_resume_ack(self, msg_type, sender_id, target_id, payload):
        relay = build_frame(MSG_FILE_RESUME_ACK, sender_id, target_id, payload)
        self._server.send_to(target_id, relay, MSG_FILE_RESUME_ACK)

    def _handle_chunk(self, msg_type, sender_id, target_id, payload):
        if target_id == HOST_ID: return
        relay = build_frame(MSG_FILE_CHUNK, sender_id, target_id, payload)
        self._server.send_to(target_id, relay, MSG_FILE_CHUNK)

    def send_file(self, file_path: str, target_id: int):
        sender = HostFileSender(self._server, [file_path], target_id, HOST_ID, False)
        self.register_sender(sender)
        sender.start()

    def send_folder(self, folder_path: str, target_id: int):
        file_list = []
        for root, _, files in os.walk(folder_path):
            for f in files: file_list.append(os.path.join(root, f))
        sender = HostFileSender(self._server, file_list, target_id, HOST_ID, True, os.path.basename(folder_path), folder_path)
        self.register_sender(sender)
        sender.start()


class HostFileSender:
    """Host 作为发送方 (支持动态重建)"""
    def __init__(self, server, abs_paths=None, target_id=0, sender_id=0, is_folder=False, base_name="", root_path="", task_dict=None):
        self._server = server
        self._resume_event = threading.Event()
        
        if task_dict:
            self._task = task_dict
            self._files = task_dict["files"]
            self.task_id = task_dict["task_id"]
            self._target_id = task_dict["target_id"]
        else:
            self.task_id = int(time.time() * 1000) & 0xFFFFFFFF
            self._target_id = target_id
            self._files = []
            for p in abs_paths:
                size = os.path.getsize(p)
                mtime = os.path.getmtime(p)
                fp = hashlib.md5(f"{size}_{mtime}".encode()).hexdigest()[:8]
                rel = os.path.relpath(p, root_path) if root_path else os.path.basename(p)
                self._files.append({
                    "abs_path": p.replace("\\", "/"), "rel_path": rel.replace("\\", "/"),
                    "size": size, "fingerprint": fp, "received_bytes": 0, "status": "pending"
                })
            self._task = {"task_id": self.task_id, "sender_id": sender_id, "target_id": target_id, "is_folder": is_folder, "base_name": base_name, "files": self._files}

    @classmethod
    def from_json(cls, server, task_dict):
        return cls(server, task_dict=task_dict)

    def start(self):
        meta_payload = json.dumps(self._task).encode("utf-8")
        meta_frame = build_frame(MSG_FILE_TASK_META, HOST_ID, self._target_id, meta_payload)
        self._server.send_to(self._target_id, meta_frame, MSG_FILE_TASK_META)
        threading.Thread(target=self._wait_and_send, daemon=True).start()

    def _wait_and_send(self):
        if self._resume_event.wait(timeout=60):
            ack_frame = build_frame(MSG_FILE_RESUME_ACK, HOST_ID, self._target_id, b"\x00")
            self._server.send_to(self._target_id, ack_frame, MSG_FILE_RESUME_ACK)
            self._send_chunks()

    def handle_resume_req(self, payload):
        try:
            remote_task = json.loads(payload.decode("utf-8"))
            for rf in remote_task["files"]:
                for lf in self._files:
                    if lf["abs_path"] == rf["abs_path"] and lf["fingerprint"] == rf["fingerprint"]:
                        lf["received_bytes"] = rf["received_bytes"]
                        break
            self._resume_event.set()
        except Exception as e:
            log.error(TAG, f"Parse resume req error: {e}")

    def _send_chunks(self):
        for idx, file_info in enumerate(self._files):
            if file_info["status"] == "completed": continue
            try:
                with open(file_info["abs_path"], "rb") as f:
                    f.seek(file_info["received_bytes"])
                    while True:
                        chunk = f.read(config.FILE_CHUNK_SIZE)
                        if not chunk: break
                        header = struct.pack("!II", self.task_id, idx)
                        chunk_frame = build_frame(MSG_FILE_CHUNK, HOST_ID, self._target_id, header + chunk)
                        self._server.send_to(self._target_id, chunk_frame, MSG_FILE_CHUNK)
            except Exception as e:
                log.error(TAG, f"Send chunk error: {e}")
                break
