# -*- coding: utf-8 -*-
"""
语音模块 —— 房客端音频收发（TCP 传输）
职责：
  1. 麦克风采集 → 通过 ClientConnection TCP 发送给房主
  2. 从房主接收混合音频（TCP）→ 扬声器播放
"""

import queue
import threading
from typing import Optional

import pyaudio

from common import logger as log
import config

TAG = "GuestAudio"


class GuestAudio:
    """
    房客音频处理器（TCP 模式）。
    - 麦克风采集通过 ClientConnection TCP 发送给房主
    - 从房主接收混合音频并通过独立播放线程播放
    """

    def __init__(self, client_conn, client_id: int = 0):
        """
        :param client_conn: ClientConnection 实例，用于 TCP 收发
        :param client_id: 本房客被分配的 ID
        """
        self._client_conn = client_conn
        self._client_id = client_id
        self._running = False
        self._pa: Optional[pyaudio.PyAudio] = None
        self._input_stream: Optional[pyaudio.Stream] = None
        self._output_stream: Optional[pyaudio.Stream] = None
        self._send_thread: Optional[threading.Thread] = None
        self._play_thread: Optional[threading.Thread] = None
        self._play_queue: queue.Queue = queue.Queue(maxsize=50)
        self._mic_on = False

    @property
    def mic_on(self) -> bool:
        return self._mic_on

    def set_client_id(self, cid: int) -> None:
        self._client_id = cid

    # ═════════════════════════════════════════
    # 扬声器输出（加入房间后即可打开，无需开麦）
    # ═════════════════════════════════════════

    def open_output(self) -> bool:
        """打开扬声器输出流 + 播放线程（加入房间后调用）。"""
        if self._output_stream is not None:
            return True
        try:
            if self._pa is None:
                self._pa = pyaudio.PyAudio()
            self._output_stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=config.AUDIO_CHANNELS,
                rate=config.AUDIO_RATE,
                output=True,
                frames_per_buffer=config.AUDIO_CHUNK
            )
            self._running = True
            # 启动播放线程
            self._play_thread = threading.Thread(
                target=self._play_loop, daemon=True, name="GuestAudioPlay"
            )
            self._play_thread.start()
            log.log(TAG, "Output stream opened (playback ready)")
            return True
        except Exception as e:
            log.error(TAG, f"Output init error: {e}")
            return False

    # ═════════════════════════════════════════
    # 麦克风（开麦时才启动发送）
    # ═════════════════════════════════════════

    def start_mic(self) -> bool:
        """开启麦克风，开始发送音频。"""
        if self._mic_on:
            return True
        # 确保输出已打开
        if not self.open_output():
            log.error(TAG, "Cannot start mic: output not ready")
            return False
        try:
            if self._input_stream is None:
                self._input_stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=config.AUDIO_CHANNELS,
                    rate=config.AUDIO_RATE,
                    input=True,
                    frames_per_buffer=config.AUDIO_CHUNK
                )
            self._mic_on = True

            # 启动发送线程
            self._send_thread = threading.Thread(
                target=self._send_loop, daemon=True, name="GuestAudioSend"
            )
            self._send_thread.start()

            log.log(TAG, "Mic started (TCP mode)")
            return True

        except Exception as e:
            log.error(TAG, f"Mic init error: {e}")
            return False

    def stop_mic(self) -> None:
        """关闭麦克风（停止发送，但保持输出流以便继续收听）。"""
        self._mic_on = False
        if self._input_stream:
            try:
                self._input_stream.stop_stream()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None
        if self._send_thread:
            self._send_thread.join(timeout=2)
            self._send_thread = None
        log.log(TAG, "Mic stopped (output still active)")

    def stop(self) -> None:
        """停止所有音频收发。"""
        self._running = False
        self._mic_on = False

        if self._input_stream:
            try:
                self._input_stream.stop_stream()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None

        if self._output_stream:
            try:
                self._output_stream.stop_stream()
                self._output_stream.close()
            except Exception:
                pass
            self._output_stream = None

        if self._pa:
            self._pa.terminate()
            self._pa = None

        if self._send_thread:
            self._send_thread.join(timeout=2)
        if self._play_thread:
            self._play_thread.join(timeout=2)

        # 清空播放队列
        while not self._play_queue.empty():
            try:
                self._play_queue.get_nowait()
            except queue.Empty:
                break

        log.log(TAG, "Audio stopped")

    # ═════════════════════════════════════════
    # 发送循环（麦克风 → TCP → 房主）
    # ═════════════════════════════════════════

    def _send_loop(self) -> None:
        log.log(TAG, "Send loop started (TCP)")
        count = 0
        while self._running and self._mic_on:
            try:
                pcm_data = self._input_stream.read(
                    config.AUDIO_CHUNK, exception_on_overflow=False
                )
                # 通过 ClientConnection TCP 发送语音帧
                self._client_conn.send_frame(0x06, 0, pcm_data)  # MSG_VOICE=0x06, target=HOST
                count += 1
                if count % 50 == 1:
                    import numpy as np
                    rms = np.sqrt(np.mean(np.frombuffer(pcm_data, dtype=np.int16).astype(np.float64) ** 2))
                    log.log(TAG, f"[SEND] pkt#{count} rms={rms:.0f}")
            except Exception as e:
                if self._running and self._mic_on:
                    log.error(TAG, f"Audio send error: {e}")
                break
        log.log(TAG, f"Send loop stopped (total={count})")

    # ═════════════════════════════════════════
    # 播放循环（独立线程，不阻塞 Qt 主线程）
    # ═════════════════════════════════════════

    def _play_loop(self) -> None:
        """播放线程：从队列取音频数据写入扬声器。"""
        log.log(TAG, "Playback loop started")
        count = 0
        while self._running:
            try:
                pcm_data = self._play_queue.get(timeout=0.005)
                if pcm_data is None:
                    break
                if self._output_stream:
                    self._output_stream.write(pcm_data)
                count += 1
                if count % 50 == 1:
                    import numpy as np
                    rms = np.sqrt(np.mean(np.frombuffer(pcm_data, dtype=np.int16).astype(np.float64) ** 2))
                    log.log(TAG, f"[PLAY] pkt#{count} rms={rms:.0f}")
            except queue.Empty:
                continue
            except Exception as e:
                if self._running:
                    log.error(TAG, f"Playback error: {e}")
                break
        log.log(TAG, f"Playback loop stopped (total={count})")

    def play_mixed_audio(self, pcm_data: bytes) -> None:
        """将混合音频放入播放队列（由 Qt 主线程调用，非阻塞）。"""
        if not self._running:
            return
        try:
            if self._play_queue.full():
                try:
                    self._play_queue.get_nowait()  # 丢弃最旧的
                except queue.Empty:
                    pass
            self._play_queue.put_nowait(pcm_data)
        except queue.Full:
            pass
        # 诊断计数
        if not hasattr(self, '_recv_count'):
            self._recv_count = 0
        self._recv_count += 1
        if self._recv_count % 50 == 1:
            log.log(TAG, f"[RECV-FROM-HOST] pkt#{self._recv_count} len={len(pcm_data)} qsize={self._play_queue.qsize()}")
