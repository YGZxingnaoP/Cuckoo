# -*- coding: utf-8 -*-
"""
投屏模块 —— 主机端（1对N 广播）
使用 mss 截屏 + OpenCV 缩放编码为 JPEG，通过 Server 广播给所有房客。
"""

import time
import threading
from typing import Optional

import cv2
import numpy as np
import mss

from common import logger as log
from core.server import Server
from core.protocol import build_frame, MSG_SCREEN_FRAME, BROADCAST_ID, HOST_ID
import config

TAG = "ScreenHost"


class ScreenHost:
    """
    主机投屏采集器。
    采集屏幕 → JPEG 编码 → 通过 Server.broadcast() 分发给所有房客。
    """

    def __init__(self, server: Server):
        self._server = server
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sct: Optional[mss.mss] = None
        # 当前分辨率和帧率设置
        preset = config.SCREEN_PRESETS[config.DEFAULT_SCREEN_PRESET]
        self._target_w: int = preset[1]
        self._target_h: int = preset[2]
        fps_preset = config.FPS_PRESETS[config.DEFAULT_FPS_PRESET]
        self._fps: int = fps_preset[1]
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    def set_resolution(self, width: int, height: int) -> None:
        """动态设置输出分辨率。(0,0) 表示原画。"""
        with self._lock:
            self._target_w = width
            self._target_h = height
        log.log(TAG, f"Resolution set to {width}x{height}" if height > 0 else "Resolution set to native")

    def set_fps(self, fps: int) -> None:
        """动态设置采集帧率。"""
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
        log.log(TAG, "Screen host started (broadcasting)")

    def _capture_loop(self) -> None:
        sct = None
        try:
            sct = mss.mss()
            monitor = sct.monitors[1]  # 主显示器
            while self._running:
                t0 = time.perf_counter()

                # 读取当前设置
                with self._lock:
                    tw, th = self._target_w, self._target_h
                    fps = self._fps
                interval = 1.0 / fps if fps > 0 else 1.0 / 15

                # 截屏
                raw = sct.grab(monitor)
                img = np.array(raw)[:, :, :3]  # BGRA -> BGR

                # 缩放
                if th > 0 and tw > 0:
                    img = cv2.resize(img, (tw, th))
                # 原画模式不缩放

                # JPEG 编码
                _, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, config.JPEG_QUALITY])
                frame_data = jpeg.tobytes()

                # 构建帧并广播
                frame = build_frame(MSG_SCREEN_FRAME, HOST_ID, BROADCAST_ID, frame_data)
                self._server.broadcast(frame)

                # 帧率控制
                elapsed = time.perf_counter() - t0
                sleep_time = interval - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except Exception as e:
            log.error(TAG, f"Screen capture error: {e}")
        finally:
            # 在创建 sct 的线程中关闭，避免跨线程 ReleaseDC 错误
            if sct:
                try:
                    sct.close()
                except Exception:
                    pass
            self._sct = None
            self._running = False
            log.log(TAG, "Screen host stopped")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        log.log(TAG, "Screen host resources released")
