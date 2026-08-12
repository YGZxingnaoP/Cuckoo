# -*- coding: utf-8 -*-
"""
投屏模块 —— 主机端（1对N 广播）
使用 dxcam (DirectX) 截屏 + turbojpeg 编码，通过 Server 广播给所有房客。
"""

import time
import threading
import queue
import zlib
from typing import Optional

import cv2
import numpy as np
import turbojpeg

from common import logger as log
from core.server import Server
from core.protocol import build_frame, MSG_SCREEN_FRAME, BROADCAST_ID, HOST_ID
import config

TAG = "ScreenHost"


class ScreenHost:
    """
    主机投屏采集器。
    采集屏幕 → turbojpeg 编码 → 通过 Server.broadcast() 分发给所有房客。
    """

    def __init__(self, server: Server):
        self._server = server
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._camera = None  # dxcam output

        preset = config.SCREEN_PRESETS[config.DEFAULT_SCREEN_PRESET]
        self._target_w: int = preset[1]
        self._target_h: int = preset[2]
        fps_preset = config.FPS_PRESETS[config.DEFAULT_FPS_PRESET]
        self._fps: int = fps_preset[1]
        self._lock = threading.Lock()

        # turbojpeg 编码器状态
        self._tj_initialized = False

    @property
    def running(self) -> bool:
        return self._running

    def set_resolution(self, width: int, height: int) -> None:
        with self._lock:
            self._target_w = width
            self._target_h = height
        log.log(TAG, f"Resolution set to {width}x{height}" if height > 0 else "Resolution set to native")

    def set_fps(self, fps: int) -> None:
        with self._lock:
            self._fps = fps
        log.log(TAG, f"FPS set to {fps}")

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="ScreenHostCapture"
        )
        self._thread.start()
        log.log(TAG, "Screen host started (dxcam + turbojpeg)")

    def _capture_loop(self) -> None:
        try:
            import dxcam
            camera = dxcam.create(output_color="BGRA")
            if camera is None:
                raise RuntimeError("dxcam.create() returned None — GPU/driver issue")
            self._camera = camera

            prev_fingerprint: int = 0
            force_send_at: float = 0.0  # 强制发送时间戳，确保静态画面定期心跳

            while self._running:
                t0 = time.perf_counter()

                with self._lock:
                    tw, th = self._target_w, self._target_h
                    fps = self._fps
                interval = 1.0 / fps if fps > 0 else 1.0 / 15

                # DirectX 截屏 → BGRA numpy array [H, W, 4]
                img = camera.grab()
                if img is None:
                    time.sleep(0.001)
                    continue

                # ── 静态画面检测 ──
                # 缩放到 32x32 缩略图取指纹（极快，<0.1ms）
                thumb = img[::max(1, img.shape[0] // 32), ::max(1, img.shape[1] // 32)]
                fp = zlib.crc32(thumb.tobytes())

                now = time.time()
                if fp == prev_fingerprint and now < force_send_at and prev_fingerprint != 0:
                    # 画面未变化且未到强制发送时间 → 跳过编码和广播
                    elapsed = time.perf_counter() - t0
                    sleep_time = interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    continue

                prev_fingerprint = fp
                force_send_at = now + 5.0  # 最多连续静默 5 秒后强行发一帧

                # 缩放（INTER_AREA 是 OpenCV 推荐的降采样算法，带抗锯齿）
                if th > 0 and tw > 0:
                    if img.shape[0] != th or img.shape[1] != tw:
                        img = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)

                # BGRA → RGB contiguous (turbojpeg 需要 RGB)
                rgb = np.ascontiguousarray(img[:, :, 2::-1])

                # turbojpeg 编码（Y444 = 无色彩压缩，屏幕共享必须保留颜色精度）
                frame_data = turbojpeg.compress(
                    rgb.tobytes(),
                    config.JPEG_QUALITY,
                    turbojpeg.SAMP.Y444,
                    turbojpeg.CS.RGB,
                    False, False, False, False, False,
                    0, 0, 1, 1, turbojpeg.DU.UNKNOWN,
                    rgb.shape[1], rgb.shape[0],
                    turbojpeg.PF.RGB,
                )

                # 广播
                frame = build_frame(MSG_SCREEN_FRAME, HOST_ID, BROADCAST_ID, frame_data)
                self._server.broadcast(frame, msg_type=MSG_SCREEN_FRAME)

                # 帧率控制
                elapsed = time.perf_counter() - t0
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except ImportError as e:
            log.error(TAG, f"dxcam not installed: {e}")
            self._running = False
        except Exception as e:
            log.error(TAG, f"Screen capture error: {e}")
        finally:
            if self._camera:
                try:
                    del self._camera
                except Exception:
                    pass
                self._camera = None
            self._running = False
            log.log(TAG, "Screen host stopped")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

        if self._server:
            with self._server._clients_lock:
                for info in self._server._clients.values():
                    while not info.media_queue.empty():
                        try:
                            info.media_queue.get_nowait()
                        except queue.Empty:
                            break
        log.log(TAG, "Screen host resources released and queues cleared")
