# -*- coding: utf-8 -*-
"""
专业级纯 Python 音频处理管道 (Audio Pipeline)
零外部依赖，基于 Numpy 向量化计算，保证极致性能与低延迟。
"""

import numpy as np
import threading
from collections import deque
from typing import Optional

class TxPipeline:
    """
    发送端管道：动态包络噪声门 (Envelope Noise Gate)
    平滑压制底噪，避免声音被生硬切断。
    """
    def __init__(self, sample_rate: int = 16000, noise_gate_threshold: float = 0.015):
        self.sample_rate = sample_rate
        self.threshold = noise_gate_threshold
        
        # 包络跟随器状态 (Envelope Follower)
        self.envelope = 0.0
        # 攻击时间 (Attack) 5ms，释放时间 (Release) 50ms
        self.attack_coeff = np.exp(-1.0 / (sample_rate * 0.005))
        self.release_coeff = np.exp(-1.0 / (sample_rate * 0.050))

    def set_noise_gate(self, threshold: float) -> None:
        """动态调整噪声门阈值 (0.0 ~ 0.1)"""
        self.threshold = max(0.001, min(0.1, threshold))

    def process(self, pcm_bytes: bytes) -> bytes:
        """处理一帧 PCM 数据 (int16 -> int16)"""
        # 1. 归一化为 float32 [-1.0, 1.0]
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        
        # 2. 计算当前帧 RMS 能量
        rms = np.sqrt(np.mean(samples**2))
        
        # 3. 包络跟随 (平滑能量变化)
        if rms > self.envelope:
            self.envelope = self.attack_coeff * self.envelope + (1 - self.attack_coeff) * rms
        else:
            self.envelope = self.release_coeff * self.envelope + (1 - self.release_coeff) * rms
            
        # 4. 计算噪声门增益 (低于阈值时平滑衰减)
        if self.envelope < self.threshold:
            # 平滑衰减曲线，避免爆音
            gain = (self.envelope / self.threshold) ** 2 
        else:
            gain = 1.0
            
        # 5. 应用增益并转回 int16
        samples *= gain
        return (samples * 32768.0).clip(-32768, 32767).astype(np.int16).tobytes()


class RxPipeline:
    """
    接收端管道：抖动缓冲 (Jitter Buffer) + AGC + 软限幅 (Soft Clipper)
    消除网络抖动电音，自动平衡音量，极限增幅不破音。
    """
    def __init__(self, sample_rate: int = 16000, chunk_size_ms: int = 20, buffer_size_ms: int = 60):
        self.sample_rate = sample_rate
        self.chunk_size_ms = chunk_size_ms
        self.buffer_size_ms = buffer_size_ms
        
        # 抖动缓冲区
        self.buffer = deque()
        self.buffer_lock = threading.Lock()
        self.chunk_samples = int(sample_rate * chunk_size_ms / 1000)
        
        # AGC (自动增益控制) 状态
        self.agc_gain = 1.0
        self.agc_target_rms = 0.15  # 目标 RMS 能量
        
        # 软限幅参数
        self.clip_drive = 1.5  # 驱动增益，越大声音越“厚实”
        self._volume_gain = 1.0

    def set_volume_gain(self, gain_percent: int) -> None:
        """【新增】设置音量增益 (50~300)"""
        self._volume_gain = max(0.1, min(3.0, gain_percent / 100.0))

    def set_buffer_size(self, buffer_size_ms: int) -> None:
        """动态调整抖动缓冲大小 (20ms ~ 200ms)"""
        self.buffer_size_ms = max(20, min(200, buffer_size_ms))

    def push(self, pcm_bytes: bytes) -> None:
        """网络接收线程调用：将音频包推入抖动缓冲"""
        with self.buffer_lock:
            self.buffer.append(pcm_bytes)
            # 限制最大缓冲深度，防止延迟累积
            max_chunks = int(self.buffer_size_ms / self.chunk_size_ms)
            while len(self.buffer) > max_chunks:
                self.buffer.popleft()

    def pull_and_process(self) -> bytes:
        """播放线程调用：从缓冲抽取一帧，并进行 AGC 和软限幅处理"""
        with self.buffer_lock:
            if len(self.buffer) > 0:
                pcm_bytes = self.buffer.popleft()
            else:
                # 缓冲为空（网络卡顿），输出静音（ Concealment ）
                return np.zeros(self.chunk_samples, dtype=np.int16).tobytes()

        # 1. 归一化
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        
        # 2. AGC (自动增益控制)
        rms = np.sqrt(np.mean(samples**2))
        if rms > 0.005:  # 忽略极小底噪
            target_gain = self.agc_target_rms / rms
            # 平滑调整增益，避免音量忽大忽小
            self.agc_gain = self.agc_gain * 0.95 + target_gain * 0.05
            self.agc_gain = np.clip(self.agc_gain, 0.5, 8.0)  # 限制最大增幅 8 倍
        
        samples *= self.agc_gain
        
        # 3. 软限幅 (Soft Clipper) - 使用 tanh 模拟电子管饱和
        # 即使增益开到 8 倍，声音也只会变得紧凑，绝对不会出现“啪啪”的破音
        samples = np.tanh(samples * self.clip_drive) / np.tanh(self.clip_drive)
        samples *= self._volume_gain

        # 4. 转回 int16
        return (samples * 32768.0).astype(np.int16).tobytes()
