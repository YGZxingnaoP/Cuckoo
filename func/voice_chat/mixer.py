# -*- coding: utf-8 -*-
"""
语音模块 —— 主机 MCU 混音器
【致命Bug修复】：实现主机回放设备的热插拔。
"""

import threading
import time
from collections import defaultdict
from typing import Optional, Callable

import numpy as np
import pyaudio

from common import logger as log
import config

from func.voice_chat.audio_pipeline import RxPipeline

TAG = "AudioMixer"
MIX_INTERVAL = 0.032

class AudioMixer:
    def __init__(self, send_callback: Optional[Callable[[int, bytes], None]] = None,
                 input_device_index: int = -1, output_device_index: int = -1):
        self._running = False
        self._mix_thread: Optional[threading.Thread] = None
        self._send_callback = send_callback
        self._input_device_index = input_device_index if input_device_index >= 0 else None
        self._output_device_index = output_device_index if output_device_index >= 0 else None

        self._buffers: dict[int, list[bytes]] = defaultdict(list)
        self._buffer_lock = threading.Lock()
        self._host_buffer: list[bytes] = []
        self._host_lock = threading.Lock()
        self._client_ids: set[int] = set()
        self._client_lock = threading.Lock()
        self._get_client_ids_callback: Optional[Callable[[], list[int]]] = None

        self._playback_buffer: list[bytes] = []
        self._playback_lock = threading.Lock()
        self._playback_thread: Optional[threading.Thread] = None
        self._playback_pa: Optional[pyaudio.PyAudio] = None
        self._playback_stream: Optional[pyaudio.Stream] = None

        self._system_buffer: list[bytes] = []
        self._system_lock = threading.Lock()

        self._volume_gain: float = 1.0
        self._gain_lock = threading.Lock()

        # 【致命Bug修复】：回放设备热插拔版本控制
        self._playback_device_version = 0
        self._current_playback_version = -1

        self.host_rx_pipeline = RxPipeline(sample_rate=config.AUDIO_RATE, chunk_size_ms=20, buffer_size_ms=40)

    def register_client(self, uid: int) -> None:
        with self._client_lock: self._client_ids.add(uid)

    def unregister_client(self, uid: int) -> None:
        with self._client_lock: self._client_ids.discard(uid)
        with self._buffer_lock: self._buffers.pop(uid, None)

    def set_send_callback(self, cb: Callable[[int, bytes], None]) -> None: self._send_callback = cb

    def set_device(self, input_device_index: int, output_device_index: int) -> None:
        self._input_device_index = input_device_index if input_device_index >= 0 else None
        self._output_device_index = output_device_index if output_device_index >= 0 else None
        self._playback_device_version += 1
        log.log(TAG, f"Host playback device change triggered (version={self._playback_device_version})")

    def set_get_client_ids_callback(self, cb: Callable[[], list[int]]) -> None: self._get_client_ids_callback = cb
    def set_volume_gain(self, gain_percent: int) -> None:
        with self._gain_lock: self._volume_gain = gain_percent / 100.0

    def start(self) -> None:
        if self._running: return
        self._running = True
        self._mix_thread = threading.Thread(target=self._mix_loop, daemon=True, name="AudioMixerMix")
        self._mix_thread.start()

    def stop(self) -> None:
        self._running = False
        self.stop_playback()
        if self._mix_thread: self._mix_thread.join(timeout=2)

    def push_client_audio(self, sender_id: int, pcm_data: bytes) -> None:
        with self._client_lock:
            if sender_id not in self._client_ids: self._client_ids.add(sender_id)
        with self._buffer_lock:
            buf = self._buffers[sender_id]
            buf.append(pcm_data)
            if len(buf) > 5: self._buffers[sender_id] = buf[-3:]

    def push_host_audio(self, pcm_data: bytes) -> None:
        with self._host_lock:
            self._host_buffer.append(pcm_data)
            if len(self._host_buffer) > 5: self._host_buffer = self._host_buffer[-3:]

    def push_system_audio(self, pcm_data: bytes) -> None:
        with self._system_lock:
            self._system_buffer.append(pcm_data)
            if len(self._system_buffer) > 5: self._system_buffer = self._system_buffer[-3:]

    def _mix_loop(self) -> None:
        while self._running:
            time.sleep(MIX_INTERVAL)
            client_audio: dict[int, bytes] = {}
            host_audio: Optional[bytes] = None

            with self._buffer_lock:
                for uid in list(self._buffers.keys()):
                    buf = self._buffers[uid]
                    if buf: client_audio[uid] = buf.pop(0)

            with self._host_lock:
                if self._host_buffer: host_audio = self._host_buffer.pop(0)

            system_audio: Optional[bytes] = None
            with self._system_lock:
                if self._system_buffer: system_audio = self._system_buffer.pop(0)

            if not client_audio and host_audio is None and system_audio is None: continue

            if self._send_callback:
                client_ids = self._get_client_ids_callback() if self._get_client_ids_callback else list(self._client_ids)
                for uid in client_ids:
                    others = [pcm for sid, pcm in client_audio.items() if sid != uid]
                    if host_audio is not None: others.append(host_audio)
                    if system_audio is not None: others.append(system_audio)
                    if others:
                        mixed = self._mix_pcm(others)
                        try: self._send_callback(uid, mixed)
                        except Exception as e: log.error(TAG, f"Send to {uid} error: {e}")

            host_mix_sources = list(client_audio.values())
            if host_mix_sources:
                host_mixed = self._mix_pcm(host_mix_sources)
                self.host_rx_pipeline.push(host_mixed)
                with self._playback_lock:
                    self._playback_buffer.append(host_mixed)
                    if len(self._playback_buffer) > 10: self._playback_buffer = self._playback_buffer[-5:]

    def _mix_pcm(self, pcm_list: list[bytes]) -> bytes:
        arrays = [np.frombuffer(p, dtype=np.int16).astype(np.int32) for p in pcm_list]
        if not arrays: return b""
        max_len = max(len(a) for a in arrays)
        for i in range(len(arrays)):
            if len(arrays[i]) < max_len: arrays[i] = np.pad(arrays[i], (0, max_len - len(arrays[i])))
        mixed = np.sum(arrays, axis=0)
        mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
        return mixed.tobytes()

    def start_playback(self) -> bool:
        if self._playback_thread and self._playback_thread.is_alive(): return True
        try:
            self._playback_pa = pyaudio.PyAudio()
            self._playback_stream = self._open_playback_stream_safe()
            if self._playback_stream is None: raise RuntimeError("Playback stream init failed")
            self._playback_thread = threading.Thread(target=self._playback_loop, daemon=True, name="HostPlayback")
            self._playback_thread.start()
            return True
        except Exception as e:
            log.error(TAG, f"Playback init error: {e}")
            self.stop_playback()
            return False

    def stop_playback(self) -> None:
        if self._playback_stream:
            try: self._playback_stream.stop_stream(); self._playback_stream.close()
            except Exception: pass
            self._playback_stream = None
        if self._playback_pa:
            self._playback_pa.terminate()
            self._playback_pa = None
        if self._playback_thread:
            self._playback_thread.join(timeout=2)
            self._playback_thread = None
        with self._playback_lock: self._playback_buffer.clear()

    def _open_playback_stream_safe(self) -> Optional[pyaudio.Stream]:
        stream_kwargs = dict(
            format=pyaudio.paInt16, channels=config.AUDIO_CHANNELS,
            rate=config.AUDIO_RATE, output=True, frames_per_buffer=config.AUDIO_CHUNK
        )
        if self._output_device_index is not None:
            stream_kwargs['output_device_index'] = self._output_device_index
        
        # 尝试打开，如果失败则回退到默认设备
        try:
            return self._playback_pa.open(**stream_kwargs)
        except Exception as e:
            log.warn(TAG, f"Playback with specific device failed: {e}, trying default.")
            stream_kwargs.pop('output_device_index', None)
            try: return self._playback_pa.open(**stream_kwargs)
            except Exception: return None

    def _rebuild_playback_if_needed(self) -> None:
        """【致命Bug修复】：在回放线程内检测版本变化，安全重建 Stream。"""
        if self._playback_device_version == self._current_playback_version:
            return
        
        log.log(TAG, "Rebuilding host playback stream...")
        if self._playback_stream:
            try: self._playback_stream.stop_stream(); self._playback_stream.close()
            except Exception: pass
        
        self._playback_stream = self._open_playback_stream_safe()
        self._current_playback_version = self._playback_device_version

    def _apply_gain(self, pcm_data: bytes) -> bytes:
        with self._gain_lock: gain = self._volume_gain
        if abs(gain - 1.0) < 0.01: return pcm_data
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.int32)
        samples = (samples * gain).clip(-32768, 32767).astype(np.int16)
        return samples.tobytes()

    def _playback_loop(self) -> None:
        """【核心重构】：主机回放线程，由 Jitter Buffer 驱动"""
        while self._running:
            self._rebuild_playback_if_needed()
            if not self._playback_stream:
                time.sleep(0.1)
                continue
            
            try:
                # 从抖动缓冲拉取，并经过 AGC + 软限幅处理
                chunk = self.host_rx_pipeline.pull_and_process()
                self._playback_stream.write(chunk)
            except Exception as e:
                if self._running: log.error(TAG, f"Playback write error: {e}")
                break