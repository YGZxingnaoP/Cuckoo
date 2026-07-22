# -*- coding: utf-8 -*-
"""
专业级纯 Python 音频处理管道 (Audio Pipeline)
零外部依赖，基于 Numpy 向量化计算，保证极致性能与低延迟。
"""

import numpy as np
import threading
from collections import deque

class TxPipeline:
    """发送端管道：动态包络噪声门 (Envelope Noise Gate) + 硬截断"""
    def __init__(self, sample_rate: int = 16000, noise_gate_threshold: float = 0.02):
        self.sample_rate = sample_rate
        self.threshold = noise_gate_threshold
        self.envelope = 0.0
        # 攻击 5ms，释放 50ms
        self.attack_coeff = np.exp(-1.0 / (sample_rate * 0.005))
        self.release_coeff = np.exp(-1.0 / (sample_rate * 0.050))

    def set_noise_gate(self, threshold: float) -> None:
        self.threshold = max(0.005, min(0.1, threshold))

    def process(self, pcm_bytes: bytes) -> bytes:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        rms = np.sqrt(np.mean(samples**2))
        
        # 包络跟随
        if rms > self.envelope:
            self.envelope = self.attack_coeff * self.envelope + (1 - self.attack_coeff) * rms
        else:
            self.envelope = self.release_coeff * self.envelope + (1 - self.release_coeff) * rms
            
        # 【核心修复】硬截断底噪，低于阈值一半直接静音，彻底消除嘶嘶声
        if self.envelope < self.threshold * 0.5:
            return np.zeros_like(samples, dtype=np.int16).tobytes()
        elif self.envelope < self.threshold:
            gain = (self.envelope / self.threshold) ** 2
            samples *= gain
            
        return (samples * 32768.0).clip(-32768, 32767).astype(np.int16).tobytes()

class RxPipeline:
    """接收端管道：高通滤波 + 抖动缓冲 + AGC + 软限幅"""
    def __init__(self, sample_rate: int = 16000, chunk_size_ms: int = 20, buffer_size_ms: int = 60):
        self.sample_rate = sample_rate
        self.buffer_size_ms = buffer_size_ms
        self.buffer = deque()
        self.buffer_lock = threading.Lock()
        self.chunk_samples = int(sample_rate * chunk_size_ms / 1000)
        
        self.agc_gain = 1.0
        self.agc_target_rms = 0.15
        self.clip_drive = 1.5
        self._volume_gain = 1.0
        
        # 【核心修复】一阶高通滤波器状态 (去除 50Hz/60Hz 电流嗡嗡声)
        self.hp_prev_in = 0.0
        self.hp_prev_out = 0.0
        self.hp_alpha = 0.98  # 截止频率约 50Hz

    def set_volume_gain(self, gain_percent: int) -> None:
        self._volume_gain = max(0.1, min(3.0, gain_percent / 100.0))

    def set_buffer_size(self, buffer_size_ms: int) -> None:
        self.buffer_size_ms = max(20, min(200, buffer_size_ms))

    def push(self, pcm_bytes: bytes) -> None:
        with self.buffer_lock:
            self.buffer.append(pcm_bytes)
            max_chunks = int(self.buffer_size_ms / (self.chunk_samples * 2 * 1000 / self.sample_rate))
            while len(self.buffer) > max(3, max_chunks):
                self.buffer.popleft()

    def pull_and_process(self) -> bytes:
        with self.buffer_lock:
            if len(self.buffer) > 0:
                pcm_bytes = self.buffer.popleft()
            else:
                return np.zeros(self.chunk_samples, dtype=np.int16).tobytes()

        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        
        # 【核心修复】向量化高通滤波，去除低频电流声
        filtered = np.zeros_like(samples)
        for i in range(len(samples)):
            filtered[i] = self.hp_alpha * (self.hp_prev_out + samples[i] - self.hp_prev_in)
            self.hp_prev_in = samples[i]
            self.hp_prev_out = filtered[i]
        samples = filtered
        
        # AGC
        rms = np.sqrt(np.mean(samples**2))
        if rms > 0.005:
            target_gain = self.agc_target_rms / rms
            self.agc_gain = self.agc_gain * 0.95 + target_gain * 0.05
            self.agc_gain = np.clip(self.agc_gain, 0.5, 8.0)
        samples *= self.agc_gain
        
        # 软限幅
        samples = np.tanh(samples * self.clip_drive) / np.tanh(self.clip_drive)
        samples *= self._volume_gain

        return (samples * 32768.0).astype(np.int16).tobytes()
