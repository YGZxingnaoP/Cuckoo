# -*- coding: utf-8 -*-
"""
语音模块 —— 主机 MCU 混音器（TCP 传输）
职责：
  1. 接收所有房客（TCP 推送）+ 主机自身的音频
  2. 按时间片混合多路 PCM（相加 + 截断）
  3. 将混合后的音频通过 TCP 发送给所有房客
"""

import threading
import time
from collections import defaultdict
from typing import Optional, Callable

import numpy as np
import pyaudio

from common import logger as log
import config

TAG = "AudioMixer"

# 混音时间片（秒）—— 严格匹配 AUDIO_CHUNK / AUDIO_RATE = 1024/16000 = 64ms
MIX_INTERVAL = 0.064  # 64ms（与采集帧长一致，避免缓冲区耗尽/溢出）


class AudioMixer:
    """
    主机端集中混音器（MCU）。
    - 接收所有音频（由 main_window 通过 push_client_audio 推送）
    - 每 20ms 混合所有音源
    - 通过 send_callback 将混合音频发给房客
    """

    def __init__(self, send_callback: Optional[Callable[[int, bytes], None]] = None):
        """
        :param send_callback: fn(client_uid, pcm_data) 用于向房客发送混合音频
        """
        self._running = False
        self._mix_thread: Optional[threading.Thread] = None
        self._send_callback: Optional[Callable[[int, bytes], None]] = send_callback

        # 多路音频缓冲：{sender_id: [pcm_chunk, ...]}
        self._buffers: dict[int, list[bytes]] = defaultdict(list)
        self._buffer_lock = threading.Lock()

        # 主机自身麦克风缓冲
        self._host_buffer: list[bytes] = []
        self._host_lock = threading.Lock()

        # 已连接的客户端 ID 集合
        self._client_ids: set[int] = set()
        self._client_lock = threading.Lock()

        # 主机回放（扬声器播放混合音频）
        self._playback_buffer: list[bytes] = []
        self._playback_lock = threading.Lock()
        self._playback_thread: Optional[threading.Thread] = None
        self._playback_pa: Optional[pyaudio.PyAudio] = None
        self._playback_stream: Optional[pyaudio.Stream] = None

        # 音量增益
        self._volume_gain: float = 1.0
        self._gain_lock = threading.Lock()

    # ═════════════════════════════════════════
    # 客户端管理
    # ═════════════════════════════════════════

    def register_client(self, uid: int) -> None:
        """注册客户端。"""
        with self._client_lock:
            self._client_ids.add(uid)
        log.log(TAG, f"Registered client {uid}")

    def unregister_client(self, uid: int) -> None:
        """移除客户端。"""
        with self._client_lock:
            self._client_ids.discard(uid)
        with self._buffer_lock:
            self._buffers.pop(uid, None)
        log.log(TAG, f"Unregistered client {uid}")

    def set_send_callback(self, cb: Callable[[int, bytes], None]) -> None:
        """设置发送回调。"""
        self._send_callback = cb

    def set_volume_gain(self, gain_percent: int) -> None:
        """设置主机回放音量增益（百分比，100=原始）。"""
        with self._gain_lock:
            self._volume_gain = gain_percent / 100.0
        log.log(TAG, f"Host volume gain set to {gain_percent}% ({gain_percent / 100.0:.2f}x)")

    # ═════════════════════════════════════════
    # 启动与停止
    # ═════════════════════════════════════════

    def start(self) -> None:
        """启动混音器。"""
        if self._running:
            return

        self._running = True

        self._mix_thread = threading.Thread(
            target=self._mix_loop, daemon=True, name="AudioMixerMix"
        )
        self._mix_thread.start()

        log.log(TAG, "Audio mixer started (TCP mode)")

    def stop(self) -> None:
        """停止混音器。"""
        self._running = False

        # 停止回放
        self.stop_playback()

        if self._mix_thread:
            self._mix_thread.join(timeout=2)

        log.log(TAG, "Audio mixer stopped")

    # ═════════════════════════════════════════
    # 音频输入（由 main_window 调用）
    # ═════════════════════════════════════════

    def push_client_audio(self, sender_id: int, pcm_data: bytes) -> None:
        """接收来自房客的音频（由 Server handler 调用）。"""
        # 自动注册
        with self._client_lock:
            if sender_id not in self._client_ids:
                self._client_ids.add(sender_id)
                log.log(TAG, f"Auto-registered client {sender_id} from voice data")

        with self._buffer_lock:
            buf = self._buffers[sender_id]
            buf.append(pcm_data)
            if len(buf) > 5:
                self._buffers[sender_id] = buf[-3:]

    def push_host_audio(self, pcm_data: bytes) -> None:
        """主机麦克风采集数据入缓冲。"""
        with self._host_lock:
            self._host_buffer.append(pcm_data)
            if len(self._host_buffer) > 5:
                self._host_buffer = self._host_buffer[-3:]

    # ═════════════════════════════════════════
    # 混音循环
    # ═════════════════════════════════════════

    def _mix_loop(self) -> None:
        """混音线程：每 60ms 混合所有音源并发送（排除各客户端自身音频）。"""
        log.log(TAG, "Mix loop started")
        count = 0
        while self._running:
            time.sleep(MIX_INTERVAL)

            # 取出所有缓冲，记录来源 ID
            client_audio: dict[int, bytes] = {}  # {client_uid: pcm}
            host_audio: Optional[bytes] = None

            with self._buffer_lock:
                for uid in list(self._buffers.keys()):
                    buf = self._buffers[uid]
                    if buf:
                        client_audio[uid] = buf.pop(0)

            with self._host_lock:
                if self._host_buffer:
                    host_audio = self._host_buffer.pop(0)

            if not client_audio and host_audio is None:
                continue

            # 为每个客户端生成排除自身音频的混合
            if self._send_callback:
                with self._client_lock:
                    client_ids = list(self._client_ids)
                for uid in client_ids:
                    others = [pcm for sid, pcm in client_audio.items() if sid != uid]
                    if host_audio is not None:
                        others.append(host_audio)
                    if others:
                        mixed = self._mix_pcm(others)
                        rms = np.sqrt(np.mean(np.frombuffer(mixed, dtype=np.int16).astype(np.float64) ** 2))
                        log.log(TAG, f"[MIX->C{uid}] others={[sid for sid in client_audio if sid != uid]}+host={host_audio is not None} rms={rms:.0f}")
                        try:
                            self._send_callback(uid, mixed)
                        except Exception as e:
                            log.error(TAG, f"Send to {uid} error: {e}")

            # 主机回放：播放所有客户端音频（不含主机自身麦克风）
            host_mix_sources = list(client_audio.values())
            if host_mix_sources:
                host_mixed = self._mix_pcm(host_mix_sources)
                with self._playback_lock:
                    self._playback_buffer.append(host_mixed)
                    if len(self._playback_buffer) > 10:
                        self._playback_buffer = self._playback_buffer[-5:]

            count += 1
            if count % 50 == 1:
                with self._client_lock:
                    n_clients = len(self._client_ids)
                log.log(TAG, f"[MIX] cycle#{count} client_srcs={list(client_audio.keys())} host_src={host_audio is not None} registered={n_clients}")

        log.log(TAG, f"Mix loop stopped (total={count})")

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

    # _broadcast_mixed 已内联到 _mix_loop 中（每个客户端收到排除自身的混合）

    # ═════════════════════════════════════════
    # 主机音频回放
    # ═════════════════════════════════════════

    def start_playback(self) -> bool:
        """启动主机音频回放（播放混合音频到扬声器）。"""
        if self._playback_thread and self._playback_thread.is_alive():
            return True
        try:
            self._playback_pa = pyaudio.PyAudio()
            self._playback_stream = self._playback_pa.open(
                format=pyaudio.paInt16,
                channels=config.AUDIO_CHANNELS,
                rate=config.AUDIO_RATE,
                output=True,
                frames_per_buffer=config.AUDIO_CHUNK
            )
            self._playback_thread = threading.Thread(
                target=self._playback_loop, daemon=True, name="HostPlayback"
            )
            self._playback_thread.start()
            log.log(TAG, "Host playback started")
            return True
        except Exception as e:
            log.error(TAG, f"Playback init error: {e}")
            self.stop_playback()
            return False

    def stop_playback(self) -> None:
        """停止主机音频回放。"""
        if self._playback_stream:
            try:
                self._playback_stream.stop_stream()
                self._playback_stream.close()
            except Exception:
                pass
            self._playback_stream = None
        if self._playback_pa:
            self._playback_pa.terminate()
            self._playback_pa = None
        if self._playback_thread:
            self._playback_thread.join(timeout=2)
            self._playback_thread = None
        with self._playback_lock:
            self._playback_buffer.clear()
        log.log(TAG, "Host playback stopped")

    def _apply_gain(self, pcm_data: bytes) -> bytes:
        """对 PCM 数据应用音量增益。"""
        with self._gain_lock:
            gain = self._volume_gain
        if abs(gain - 1.0) < 0.01:
            return pcm_data
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.int32)
        samples = (samples * gain).clip(-32768, 32767).astype(np.int16)
        return samples.tobytes()

    def _playback_loop(self) -> None:
        """主机回放线程：从缓冲区播放混合音频到扬声器。"""
        log.log(TAG, "Playback loop started")
        while self._running:
            chunk = None
            with self._playback_lock:
                if self._playback_buffer:
                    chunk = self._playback_buffer.pop(0)
            if chunk is None:
                time.sleep(0.005)
                continue
            try:
                if self._playback_stream:
                    chunk = self._apply_gain(chunk)
                    self._playback_stream.write(chunk)
            except Exception as e:
                if self._running:
                    log.error(TAG, f"Playback write error: {e}")
                break
        log.log(TAG, "Playback loop stopped")
