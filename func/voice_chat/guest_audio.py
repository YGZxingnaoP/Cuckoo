# -*- coding: utf-8 -*-
"""
语音模块 —— 房客端音频收发
职责：
  1. 麦克风采集 → UDP 发送给房主
  2. 从房主接收混合音频 → 扬声器播放
"""

import socket
import threading
from typing import Optional

import pyaudio

from common import logger as log
from core.protocol import build_voice_packet, parse_voice_packet
import config

TAG = "GuestAudio"


class GuestAudio:
    """
    房客音频处理器。
    - 麦克风采集通过 UDP 发送给房主
    - 从房主接收混合音频并播放
    """

    def __init__(self, host_ip: str, client_id: int = 0):
        self._host_ip = host_ip
        self._client_id = client_id
        self._running = False
        self._pa: Optional[pyaudio.PyAudio] = None
        self._input_stream: Optional[pyaudio.Stream] = None
        self._output_stream: Optional[pyaudio.Stream] = None
        # 单一 UDP socket：双向通信（发送麦克风 + 接收混合音频）
        self._sock: Optional[socket.socket] = None
        self._send_thread: Optional[threading.Thread] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._seq = 0
        self._mic_on = False

    @property
    def mic_on(self) -> bool:
        return self._mic_on

    def set_client_id(self, cid: int) -> None:
        self._client_id = cid

    # ═════════════════════════════════════════
    # 启动与停止
    # ═════════════════════════════════════════

    def start_mic(self) -> bool:
        """开启麦克风，开始发送和接收音频。"""
        if self._running:
            return True
        try:
            self._pa = pyaudio.PyAudio()

            # 麦克风输入流
            self._input_stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=config.AUDIO_CHANNELS,
                rate=config.AUDIO_RATE,
                input=True,
                frames_per_buffer=config.AUDIO_CHUNK
            )

            # 扬声器输出流
            self._output_stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=config.AUDIO_CHANNELS,
                rate=config.AUDIO_RATE,
                output=True,
                frames_per_buffer=config.AUDIO_CHUNK
            )

            # 单一 UDP socket（双向：发送麦克风 + 接收混合音频）
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(("0.0.0.0", 0))
            self._sock.settimeout(1.0)
            log.log(TAG, f"UDP socket bound to port {self._sock.getsockname()[1]}")

            self._running = True
            self._mic_on = True

            # 立即发送一个“注册包”让主机 mixer 发现此 socket 的地址
            self._send_hello()

            # 启动发送线程
            self._send_thread = threading.Thread(
                target=self._send_loop, daemon=True, name="GuestAudioSend"
            )
            self._send_thread.start()

            # 启动接收线程
            self._recv_thread = threading.Thread(
                target=self._recv_loop, daemon=True, name="GuestAudioRecv"
            )
            self._recv_thread.start()

            log.log(TAG, f"Audio started -> {self._host_ip}:{config.UDP_VOICE_PORT}")
            return True

        except Exception as e:
            log.error(TAG, f"Audio init error: {e}")
            self.stop()
            return False

    def stop(self) -> None:
        """停止音频收发。"""
        self._running = False
        self._mic_on = False

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

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
        if self._recv_thread:
            self._recv_thread.join(timeout=2)

        log.log(TAG, "Audio stopped")

    # ═════════════════════════════════════════
    # 发送循环（麦克风 → UDP → 房主）
    # ═════════════════════════════════════════

    def _send_loop(self) -> None:
        log.log(TAG, "Send loop started")
        silent_count = 0
        while self._running:
            try:
                pcm_data = self._input_stream.read(
                    config.AUDIO_CHUNK, exception_on_overflow=False
                )
                # VAD：静音时每 5 帧发送一次保活，有声音时始终发送
                is_silent = self._is_silent(pcm_data)
                if is_silent:
                    silent_count += 1
                    if silent_count < 5:
                        continue
                    silent_count = 0
                else:
                    silent_count = 0
                pkt = build_voice_packet(self._client_id, self._seq, pcm_data)
                self._seq = (self._seq + 1) & 0xFFFF
                self._sock.sendto(pkt, (self._host_ip, config.UDP_VOICE_PORT))
            except OSError as e:
                if self._running:
                    log.error(TAG, f"Audio send error: {e}")
                break
        log.log(TAG, "Send loop stopped")

    # ═════════════════════════════════════════
    # 接收循环（房主混合音频 → 扬声器）
    # ═════════════════════════════════════════

    def _recv_loop(self) -> None:
        log.log(TAG, "Recv loop started")
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
                result = parse_voice_packet(data)
                if result is None:
                    continue
                sender_id, seq, pcm_data = result
                self._output_stream.write(pcm_data)
            except socket.timeout:
                continue
            except OSError as e:
                if self._running:
                    log.error(TAG, f"Audio recv/play error: {e}")
                break
        log.log(TAG, "Recv loop stopped")

    # ═════════════════════════════════════════
    # 工具方法
    # ═════════════════════════════════════════

    def _send_hello(self) -> None:
        """发送一个静音包，让主机 mixer 立即发现此 socket 的 UDP 地址。"""
        try:
            silent_pcm = b"\x00" * (config.AUDIO_CHUNK * 2)  # 2 bytes per sample
            pkt = build_voice_packet(self._client_id, self._seq, silent_pcm)
            self._seq = (self._seq + 1) & 0xFFFF
            self._sock.sendto(pkt, (self._host_ip, config.UDP_VOICE_PORT))
            log.log(TAG, f"Hello packet sent from port {self._sock.getsockname()[1]}")
        except OSError as e:
            log.error(TAG, f"Hello send error: {e}")

    @staticmethod
    def _is_silent(pcm_data: bytes, threshold: int = 100) -> bool:
        """简单 VAD：判断音频是否静音。"""
        import numpy as np
        samples = np.frombuffer(pcm_data, dtype=np.int16)
        if len(samples) == 0:
            return True
        rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
        return rms < threshold
