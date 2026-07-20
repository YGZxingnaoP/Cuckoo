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

    @property
    def running(self) -> bool:
        return self._running

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
        interval = 1.0 / config.CAPTURE_FPS
        try:
            self._sct = mss.mss()
            monitor = self._sct.monitors[1]  # 主显示器
            while self._running:
                t0 = time.perf_counter()

                # 截屏
                raw = self._sct.grab(monitor)
                img = np.array(raw)[:, :, :3]  # BGRA -> BGR

                # 缩放
                img = cv2.resize(img, (config.TARGET_WIDTH, config.TARGET_HEIGHT))

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
            self._running = False
            log.log(TAG, "Screen host stopped")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._sct:
            self._sct.close()
            self._sct = None
        log.log(TAG, "Screen host resources released")
