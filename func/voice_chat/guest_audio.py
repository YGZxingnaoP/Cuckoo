# -*- coding: utf-8 -*-
"""
语音模块 —— 房客端音频收发 (集成专业音频管道)
"""

import socket
import queue
import threading
from typing import Optional

import pyaudio

from common import logger as log
from core.protocol import build_frame, MSG_VOICE, HOST_ID
from func.voice_chat.audio_pipeline import TxPipeline, RxPipeline
import config

TAG = "GuestAudio"

class GuestAudio:
    def __init__(self, client_conn, client_id: int = 0, voice_sock: Optional[socket.socket] = None,
                 input_device_index: int = -1, output_device_index: int = -1):
        self._client_conn = client_conn
        self._client_id = client_id
        self._voice_sock = voice_sock
        self._input_device_index = input_device_index if input_device_index >= 0 else None
        self._output_device_index = output_device_index if output_device_index >= 0 else None
        self._running = False
        self._pa: Optional[pyaudio.PyAudio] = None
        self._input_stream: Optional[pyaudio.Stream] = None
        self._output_stream: Optional[pyaudio.Stream] = None
        
        self._send_thread: Optional[threading.Thread] = None
        self._voice_send_thread: Optional[threading.Thread] = None
        self._voice_send_queue: queue.Queue = queue.Queue(maxsize=config.VOICE_SEND_QUEUE_MAX)
        self._play_thread: Optional[threading.Thread] = None
        self._mic_on = False
        
        # 【核心重构】：引入专业音频管道
        self.tx_pipeline = TxPipeline(sample_rate=config.AUDIO_RATE)
        self.rx_pipeline = RxPipeline(sample_rate=config.AUDIO_RATE, chunk_size_ms=20, buffer_size_ms=60)

        # 设备热插拔版本控制
        self._device_version = 0
        self._current_input_version = -1
        self._current_output_version = -1

    @property
    def mic_on(self) -> bool: return self._mic_on

    def set_device(self, input_device_index: int, output_device_index: int) -> None:
        self._input_device_index = input_device_index if input_device_index >= 0 else None
        self._output_device_index = output_device_index if output_device_index >= 0 else None
        self._device_version += 1

    def set_noise_gate(self, level: float) -> None:
        """设置降噪等级 (0.0 ~ 0.1)"""
        self.tx_pipeline.set_noise_gate(level)

    def set_jitter_buffer(self, buffer_ms: int) -> None:
        """设置抖动缓冲大小 (20 ~ 200 ms)"""
        self.rx_pipeline.set_buffer_size(buffer_ms)

    def open_output(self) -> bool:
        if self._output_stream is not None: return True
        try:
            if self._pa is None: self._pa = pyaudio.PyAudio()
            self._output_stream = self._open_stream_safe(output=True)
            if not self._output_stream: raise RuntimeError("Output init failed")
            self._running = True
            self._play_thread = threading.Thread(target=self._play_loop, daemon=True, name="GuestAudioPlay")
            self._play_thread.start()
            return True
        except Exception as e:
            log.error(TAG, f"Output init error: {e}")
            return False

    def start_mic(self) -> bool:
        if self._mic_on: return True
        if not self.open_output(): return False
        try:
            self._input_stream = self._open_stream_safe(output=False)
            if not self._input_stream: raise RuntimeError("Input init failed")
            self._mic_on = True

            self._voice_send_thread = threading.Thread(target=self._voice_net_send_loop, daemon=True)
            self._voice_send_thread.start()
            self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
            self._send_thread.start()
            return True
        except Exception as e:
            log.error(TAG, f"Mic init error: {e}")
            return False

    def stop_mic(self) -> None:
        self._mic_on = False
        if self._input_stream:
            try: self._input_stream.stop_stream(); self._input_stream.close()
            except Exception: pass
            self._input_stream = None
        if self._send_thread: self._send_thread.join(timeout=2)
        self._voice_send_queue.put(None)
        if self._voice_send_thread: self._voice_send_thread.join(timeout=2)

    def stop(self) -> None:
        self._running = False
        self._mic_on = False
        if self._input_stream:
            try: self._input_stream.stop_stream(); self._input_stream.close()
            except Exception: pass
        if self._output_stream:
            try: self._output_stream.stop_stream(); self._output_stream.close()
            except Exception: pass
        if self._pa: self._pa.terminate()
        
        if self._send_thread: self._send_thread.join(timeout=2)
        self._voice_send_queue.put(None)
        if self._voice_send_thread: self._voice_send_thread.join(timeout=2)
        if self._play_thread: self._play_thread.join(timeout=2)

    def _open_stream_safe(self, output: bool) -> Optional[pyaudio.Stream]:
        stream_kwargs = dict(
            format=pyaudio.paInt16, channels=config.AUDIO_CHANNELS,
            output=output, input=not output, frames_per_buffer=config.AUDIO_CHUNK
        )
        idx = self._output_device_index if output else self._input_device_index
        if idx is not None:
            stream_kwargs['output_device_index' if output else 'input_device_index'] = idx

        for rate in [config.AUDIO_RATE, 44100, 48000]:
            try:
                stream_kwargs['rate'] = rate
                return self._pa.open(**stream_kwargs)
            except Exception: pass
        return None

    def _rebuild_stream_if_needed(self, is_input: bool) -> None:
        if self._device_version == (self._current_input_version if is_input else self._current_output_version):
            return
        stream = self._input_stream if is_input else self._output_stream
        if stream:
            try: stream.stop_stream(); stream.close()
            except Exception: pass
        new_stream = self._open_stream_safe(output=not is_input)
        if is_input:
            self._input_stream = new_stream
            self._current_input_version = self._device_version
        else:
            self._output_stream = new_stream
            self._current_output_version = self._device_version

    def _send_loop(self) -> None:
        """采集循环：麦克风 -> TxPipeline(降噪) -> 网络队列"""
        while self._running and self._mic_on:
            self._rebuild_stream_if_needed(is_input=True)
            if not self._input_stream:
                threading.Event().wait(0.1)
                continue
            try:
                pcm_data = self._input_stream.read(config.AUDIO_CHUNK, exception_on_overflow=False)
                
                # 【核心重构】：经过发送端管道处理（降噪）
                processed_pcm = self.tx_pipeline.process(pcm_data)
                
                if self._voice_sock:
                    frame = build_frame(MSG_VOICE, self._client_id, HOST_ID, processed_pcm)
                    if self._voice_send_queue.full():
                        try: self._voice_send_queue.get_nowait()
                        except queue.Empty: pass
                    try: self._voice_send_queue.put_nowait(frame)
                    except queue.Full: pass
            except Exception as e:
                if self._running and self._mic_on: log.error(TAG, f"Audio capture error: {e}")
                break

    def _voice_net_send_loop(self) -> None:
        while self._running:
            try:
                frame = self._voice_send_queue.get(timeout=0.5)
                if frame is None: break
                if self._voice_sock: self._voice_sock.sendall(frame)
            except queue.Empty: continue
            except (ConnectionError, OSError): break

    def set_jitter_buffer(self, buffer_ms: int) -> None:
        """设置抖动缓冲大小 (20 ~ 200 ms)"""
        self.rx_pipeline.set_buffer_size(buffer_ms)

    def set_volume_gain(self, gain_percent: int) -> None:
        """【新增】设置播放音量增益"""
        self.rx_pipeline.set_volume_gain(gain_percent)

    def _play_loop(self) -> None:
        """【核心重构】：播放循环与网络接收彻底解耦，由 Jitter Buffer 驱动"""
        while self._running:
            self._rebuild_stream_if_needed(is_input=False)
            if not self._output_stream:
                threading.Event().wait(0.1)
                continue
            try:
                # 从抖动缓冲区拉取数据，并经过 RxPipeline 处理 (AGC + 软限幅)
                pcm_data = self.rx_pipeline.pull_and_process()
                self._output_stream.write(pcm_data)
            except Exception as e:
                if self._running: log.error(TAG, f"Playback error: {e}")
                break

    def play_mixed_audio(self, pcm_data: bytes) -> None:
        """网络接收线程调用：将音频推入抖动缓冲区"""
        if not self._running: return
        self.rx_pipeline.push(pcm_data)
