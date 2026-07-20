# -*- coding: utf-8 -*-
"""
语音模块 —— 主机 MCU 混音器
职责：
  1. 接收所有房客 + 主机自身的 UDP 音频
  2. 按时间片混合多路 PCM（相加 + 截断）
  3. 将混合后的音频 UDP 广播给所有房客
"""

import socket
import threading
import time
from collections import defaultdict
from typing import Optional

import numpy as np

from common import logger as log
from core.protocol import build_voice_packet, parse_voice_packet, HOST_ID
from core.server import Server
import config

TAG = "AudioMixer"

# 混音时间片（秒）
MIX_INTERVAL = 0.02  # 20ms


class AudioMixer:
    """
    主机端集中混音器（MCU）。
    - 接收所有 UDP 语音包
    - 每 20ms 混合所有音源
    - 广播混合音频给所有房客
    """

    def __init__(self, server: Server):
        self._server = server
        self._running = False
        self._sock: Optional[socket.socket] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._mix_thread: Optional[threading.Thread] = None

        # 多路音频缓冲：{sender_id: [pcm_chunk, pcm_chunk, ...]}
        self._buffers: dict[int, list[bytes]] = defaultdict(list)
        self._buffer_lock = threading.Lock()

        # 主机自身麦克风缓冲
        self._host_buffer: list[bytes] = []
        self._host_lock = threading.Lock()

        # 客户端 UDP 地址表：{client_id: (ip, port)}
        self._client_addrs: dict[int, tuple] = {}
        self._addr_lock = threading.Lock()

    # ═════════════════════════════════════════
    # 客户端地址管理
    # ═════════════════════════════════════════

    def register_client(self, uid: int, addr: tuple) -> None:
        """注册客户端的 UDP 地址。"""
        with self._addr_lock:
            self._client_addrs[uid] = addr
        log.log(TAG, f"Registered client {uid} at {addr}")

    def unregister_client(self, uid: int) -> None:
        """移除客户端。"""
        with self._addr_lock:
            self._client_addrs.pop(uid, None)
        with self._buffer_lock:
            self._buffers.pop(uid, None)
        log.log(TAG, f"Unregistered client {uid}")

    # ═════════════════════════════════════════
    # 启动与停止
    # ═════════════════════════════════════════

    def start(self) -> None:
        """启动混音器。"""
        if self._running:
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", config.UDP_VOICE_PORT))
        self._sock.settimeout(0.5)
        self._running = True

        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="AudioMixerRecv"
        )
        self._recv_thread.start()

        self._mix_thread = threading.Thread(
            target=self._mix_loop, daemon=True, name="AudioMixerMix"
        )
        self._mix_thread.start()

        log.log(TAG, f"Audio mixer started on UDP port {config.UDP_VOICE_PORT}")

    def stop(self) -> None:
        """停止混音器。"""
        self._running = False

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        if self._recv_thread:
            self._recv_thread.join(timeout=2)
        if self._mix_thread:
            self._mix_thread.join(timeout=2)

        log.log(TAG, "Audio mixer stopped")

    # ═════════════════════════════════════════
    # 接收循环
    # ═════════════════════════════════════════

    def _recv_loop(self) -> None:
        """UDP 接收线程：解析语音包，存入缓冲区。"""
        log.log(TAG, "UDP recv loop started")
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
                result = parse_voice_packet(data)
                if result is None:
                    continue
                sender_id, seq, pcm_data = result

                # 自动注册客户端地址
                with self._addr_lock:
                    if sender_id not in self._client_addrs:
                        self._client_addrs[sender_id] = addr

                # 存入缓冲
                with self._buffer_lock:
                    buf = self._buffers[sender_id]
                    buf.append(pcm_data)
                    # 限制缓冲深度（最多保留 5 个块）
                    if len(buf) > 5:
                        self._buffers[sender_id] = buf[-3:]

            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    log.error(TAG, f"UDP recv error: {e}")
        log.log(TAG, "UDP recv loop stopped")

    # ═════════════════════════════════════════
    # 混音循环
    # ═════════════════════════════════════════

    def _mix_loop(self) -> None:
        """混音线程：每 20ms 混合所有音源并广播。"""
        log.log(TAG, "Mix loop started")
        while self._running:
            time.sleep(MIX_INTERVAL)

            # 取出所有缓冲
            with self._buffer_lock:
                all_pcm: list[bytes] = []
                for uid in list(self._buffers.keys()):
                    buf = self._buffers[uid]
                    if buf:
                        all_pcm.append(buf.pop(0))

            # 加入主机自身音频
            with self._host_lock:
                if self._host_buffer:
                    all_pcm.append(self._host_buffer.pop(0))

            if not all_pcm:
                continue

            # 混合
            mixed = self._mix_pcm(all_pcm)

            # 广播
            self._broadcast_mixed(mixed)

        log.log(TAG, "Mix loop stopped")

    def push_host_audio(self, pcm_data: bytes) -> None:
        """主机麦克风采集数据入缓冲。"""
        with self._host_lock:
            self._host_buffer.append(pcm_data)
            if len(self._host_buffer) > 5:
                self._host_buffer = self._host_buffer[-3:]

    def _mix_pcm(self, pcm_list: list[bytes]) -> bytes:
        """混合多路 PCM：相加并截断。"""
        arrays = [np.frombuffer(p, dtype=np.int16).astype(np.int32) for p in pcm_list]
        if not arrays:
            return b""
        # 统一长度（零填充）
        max_len = max(len(a) for a in arrays)
        for i in range(len(arrays)):
            if len(arrays[i]) < max_len:
                arrays[i] = np.pad(arrays[i], (0, max_len - len(arrays[i])))
        mixed = np.sum(arrays, axis=0)
        mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
        return mixed.tobytes()

    def _broadcast_mixed(self, pcm_data: bytes) -> None:
        """将混合音频广播给所有已注册客户端。"""
        if not self._sock:
            return
        with self._addr_lock:
            addrs = list(self._client_addrs.items())
        for uid, addr in addrs:
            try:
                pkt = build_voice_packet(HOST_ID, 0, pcm_data)
                self._sock.sendto(pkt, addr)
            except OSError as e:
                log.error(TAG, f"Send to {uid} error: {e}")
