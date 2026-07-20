# -*- coding: utf-8 -*-
"""
投屏模块 —— 房客端接收渲染
接收 TCP 帧中的 JPEG 数据，解码为 QPixmap，通过 Signal 通知 UI。
内置"丢弃旧帧"机制，防止解码慢导致画面延迟累积。
"""

import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from common import logger as log

TAG = "ScreenGuest"


class ScreenGuest(QObject):
    """
    房客投屏接收器。
    由 ClientConnection 的 frame_received 信号驱动，在独立线程中解码。
    信号：
      - frame_ready(QPixmap): 解码完成的画面
    """

    frame_ready = Signal(QPixmap)

    def __init__(self):
        super().__init__()
        self._latest_frame: Optional[bytes] = None
        self._frame_lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动解码线程。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._decode_loop, daemon=True, name="ScreenGuestDecode"
        )
        self._thread.start()
        log.log(TAG, "Screen guest decoder started")

    def push_frame_data(self, jpeg_data: bytes) -> None:
        """接收新帧（由 MainWindow 在 frame_received 信号中调用）。"""
        with self._frame_lock:
            self._latest_frame = jpeg_data

    def _decode_loop(self) -> None:
        """解码线程：取最新帧解码，丢弃旧帧。"""
        while self._running:
            frame_data = None
            with self._frame_lock:
                if self._latest_frame is not None:
                    frame_data = self._latest_frame
                    self._latest_frame = None  # 已取走

            if frame_data is None:
                # 无新帧，短暂等待
                threading.Event().wait(0.005)
                continue

            # 解码 JPEG -> QPixmap
            pixmap = QPixmap()
            if pixmap.loadFromData(frame_data, "JPG"):
                self.frame_ready.emit(pixmap)

        log.log(TAG, "Screen guest decoder stopped")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        log.log(TAG, "Screen guest stopped")
