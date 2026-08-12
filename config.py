# -*- coding: utf-8 -*-
"""
Cuckoo 私有实时通信平台 —— 全局配置常量
星型拓扑（Star Topology）架构
严禁擅自修改端口号或核心参数。
"""

import sys
import os as _os

# ─────────────────────────────────────────────
# 网络端口（星型拓扑：主 TCP + 独立语音 TCP）
# ─────────────────────────────────────────────
TCP_PORT: int = 5000          # TCP 统一入口（信令/文本/文件/投屏）
VOICE_TCP_PORT: int = 5002   # 语音独立 TCP 通道（解决队头阻塞）

# ─────────────────────────────────────────────
# 投屏参数
# ─────────────────────────────────────────────
CAPTURE_FPS: int = 15         # 默认帧率（可动态切换）
TARGET_WIDTH: int = 1280
TARGET_HEIGHT: int = 720
JPEG_QUALITY: int = 90        # 屏幕共享推荐 85-95，文字/UI 清晰

# 分辨率预设: (名称, 宽, 高)  — 高为0表示原画
SCREEN_PRESETS: list = [
    ("720p",  1280, 720),
    ("1080p", 1920, 1080),
    ("原画质", 0, 0),
]
DEFAULT_SCREEN_PRESET: int = 0  # 默认使用第一个预设(720p)

# 帧率预设: (名称, fps)
FPS_PRESETS: list = [
    ("15 FPS", 15),
    ("30 FPS", 30),
    ("60 FPS", 60),
    ("120 FPS", 120),
]
DEFAULT_FPS_PRESET: int = 0  # 默认15fps

# ─────────────────────────────────────────────
# 语音参数（低延迟优化：端到端 < 120ms）
# ─────────────────────────────────────────────
AUDIO_CHUNK: int = 512        # 每次采集帧大小（样本数）→ 512/16000=32ms
AUDIO_RATE: int = 16000       # 采样率 Hz
AUDIO_CHANNELS: int = 1       # 单声道
VOICE_SEND_QUEUE_MAX: int = 3 # 语音发送队列上限（极小值，满则丢旧包）
VOICE_PLAY_QUEUE_MAX: int = 8 # 语音播放队列上限
MIX_INTERVAL: float = 0.032   # 混音时间片（秒）→ 32ms

# ─────────────────────────────────────────────
# 文件传输参数
# ─────────────────────────────────────────────
FILE_CHUNK_SIZE: int = 65536        # 64KB 每块（星型拓扑推荐）
FILE_SEND_DELAY: float = 0.001      # 每块发送间隔(秒)，防止占满带宽（保留兼容）
FILE_QUEUE_MAX: int = 2000          # 每客户端文件块队列上限
FILE_POOL_SIZE: int = 32 * 1024 * 1024   # 蓄水池上限：32MB（发送方未确认数据上限）
FILE_ACK_THRESHOLD: int = 2 * 1024 * 1024  # ACK 阈值：每接收 2MB 确认一次
FILE_ACK_FINAL_TIMEOUT: int = 60    # 最终ACK等待超时（秒）
FILE_POOL_STALL_TIMEOUT: int = 10   # 蓄水池停滞超时（秒），超过则强制继续防止死锁
FILE_WRITE_QUEUE_MAX: int = 512     # 写盘队列上限（内存保护）
LARGE_FILE_THRESHOLD: int = 2 * 1024 * 1024 * 1024  # 2GB：超过此大小启用MD5校验+chunk重传
MAX_RETRANSMIT_ROUNDS: int = 10     # 大文件最大重传轮数（防止无限重传）
FILE_META_BATCH_SIZE: int = 5000    # 元数据分片：每批最多文件数（约660KB，远小于16MB帧上限）

# ─────────────────────────────────────────────
# 服务端参数
# ─────────────────────────────────────────────
MAX_CLIENTS: int = 9          # 最大房客数（ID 1-9）
SEND_QUEUE_MAX: int = 100      # 每客户端信令/投屏队列上限（超过丢旧投屏帧）
ACCEPT_TIMEOUT: int = 300     # Accept 线程超时（秒）

# ─────────────────────────────────────────────
# 重连策略
# ─────────────────────────────────────────────
RECONNECT_INTERVAL: float = 5.0   # 秒
RECONNECT_MAX_RETRY: int = 3

# ─────────────────────────────────────────────
# 窗口默认尺寸
# ─────────────────────────────────────────────
WINDOW_WIDTH: int = 1024
WINDOW_HEIGHT: int = 768

# ─────────────────────────────────────────────
# 文件接收保存目录（项目内 downloads 文件夹）
# ─────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    # 如果是打包后的 exe 运行，获取 exe 所在的真实目录
    _BASE_DIR = _os.path.dirname(sys.executable)
else:
    # 如果是源码运行，获取当前文件所在目录
    _BASE_DIR = _os.path.dirname(_os.path.abspath(__file__))

DOWNLOAD_DIR: str = _os.path.join(_BASE_DIR, "downloads")

# ─────────────────────────────────────────────
# 电影院参数
# ─────────────────────────────────────────────
MOVIES_DIR: str = _os.path.join(_BASE_DIR, "movies")
CINEMA_SYNC_INTERVAL: float = 5.0    # 同步广播间隔（秒）
CINEMA_SYNC_THRESHOLD: float = 0.1   # 偏差阈值（秒），超过此值才seek校正
DEFAULT_SUBTITLE_SIZE: int = 16      # 字幕相对字号（VLC 默认16，范围10-40）