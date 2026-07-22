# -*- coding: utf-8 -*-
"""
系统音频采集模块（WASAPI Loopback）
使用 soundcard 库从系统输出设备（扬声器）捕获正在播放的音频。
用于"共享电脑声音"功能。
"""

import threading
import time
from typing import Optional, Callable

import numpy as np

from common import logger as log
import config

TAG = "SystemAudio"


class SystemAudioCapture:
    """
    系统音频捕获器。
    - 使用 soundcard WASAPI loopback 从指定扬声器设备捕获音频
    - 将捕获的 PCM 数据通过回调推送到混音器
    - 仅发送给房客（不包含在主机本地回放中，避免回声）
    """

    def __init__(self, push_callback: Optional[Callable[[bytes], None]] = None):
        """
        :param push_callback: fn(pcm_data) 每次捕获到一帧音频时调用
        """
        self._push_callback = push_callback
        self._running = False
        self._capture_thread: Optional[threading.Thread] = None
        self._device_name: Optional[str] = None  # 捕获目标扬声器名称

    def set_push_callback(self, cb: Callable[[bytes], None]) -> None:
        """设置音频推送回调。"""
        self._push_callback = cb

    def start(self, device_name: Optional[str] = None) -> bool:
        """
        开始系统音频捕获。
        :param device_name: 目标扬声器设备名称（None=默认扬声器）
        :return: 是否成功启动
        """
        if self._running:
            self.stop()

        self._device_name = device_name
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="SystemAudioCapture"
        )
        self._capture_thread.start()
        log.log(TAG, f"System audio capture started (device={device_name or 'default'})")
        return True

    def stop(self) -> None:
        """停止系统音频捕获。"""
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None
        log.log(TAG, "System audio capture stopped")

    def _capture_loop(self) -> None:
        """采集循环：从 WASAPI loopback 捕获系统音频。"""
        try:
            import soundcard as sc

            # 选择目标扬声器
            speaker = None
            if self._device_name:
                for sp in sc.all_speakers():
                    if self._device_name in sp.name or sp.name == self._device_name:
                        speaker = sp
                        break
                if speaker is None:
                    log.warn(TAG, f"Speaker '{self._device_name}' not found, using default")

            if speaker is None:
                speaker = sc.default_speaker()
                log.log(TAG, f"Using default speaker: {speaker.name}")
            else:
                log.log(TAG, f"Capturing from speaker: {speaker.name}")

            # 获取该扬声器的 loopback 麦克风
            mic = sc.get_microphone(speaker.id, include_loopback=True)
            if mic is None:
                raise RuntimeError(f"Cannot get loopback for speaker: {speaker.name}")

            # 采集参数
            sample_rate = config.AUDIO_RATE
            num_samples = config.AUDIO_CHUNK
            channels = config.AUDIO_CHANNELS

            with mic.recorder(samplerate=sample_rate, channels=channels) as recorder:
                count = 0
                while self._running:
                    # 从 loopback 读取音频帧
                    data = recorder.record(numframes=num_samples)
                    if data is not None and len(data) > 0:
                        # soundcard 返回 float32 [-1.0, 1.0]，转换为 int16 PCM
                        pcm = (data[:, 0] * 32767).astype(np.int16).tobytes()
                        if self._push_callback:
                            self._push_callback(pcm)
                        count += 1
                        if count % 50 == 1:
                            rms = np.sqrt(np.mean(data[:, 0].astype(np.float64) ** 2))
                            log.log(TAG, f"[SYS-AUDIO] pkt#{count} rms={rms:.4f}")

        except ImportError:
            log.error(TAG, "soundcard library not installed. Run: pip install soundcard")
            self._running = False
        except Exception as e:
            log.error(TAG, f"System audio capture error: {e}")
            self._running = False

        log.log(TAG, "System audio capture loop exited")

    @staticmethod
    def get_speaker_list() -> list:
        """
        获取系统所有扬声器设备列表。
        :return: [(name, name), ...] 用于 UI 下拉框
        """
        try:
            import soundcard as sc
            speakers = sc.all_speakers()
            return [(sp.name, sp.name) for sp in speakers]
        except ImportError:
            log.warn(TAG, "soundcard not installed, cannot enumerate speakers")
            return []
        except Exception as e:
            log.error(TAG, f"Failed to enumerate speakers: {e}")
            return []
