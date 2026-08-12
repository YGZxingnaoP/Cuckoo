# -*- coding: utf-8 -*-
"""
电影院模块 —— 房主端播放控制
使用 python-vlc 作为播放引擎，广播播放/暂停/跳转/同步命令。
"""

import os
import struct
import threading
import time
from typing import Optional, Callable

from PySide6.QtCore import QObject, Signal

from common import logger as log
from core.protocol import (
    build_frame, BROADCAST_ID, HOST_ID,
    MSG_CINEMA_CMD, CINEMA_PLAY, CINEMA_PAUSE, CINEMA_SEEK,
    CINEMA_SYNC, CINEMA_STOP, CINEMA_CHANGE, CINEMA_SYNC_REQ
)
import config

TAG = "CinemaHost"


class CinemaHost(QObject):
    """房主端电影院控制器"""

    status_changed = Signal(str)
    position_updated = Signal(int, int)  # current_ms, total_ms

    def __init__(self, broadcast_callback: Callable):
        """
        :param broadcast_callback: fn(frame_bytes) — 广播帧给所有房客
        """
        super().__init__()
        self._broadcast = broadcast_callback
        self._player: Optional[object] = None
        self._instance: Optional[object] = None
        self._media: Optional[object] = None
        self._playing = False
        self._paused = False
        self._current_file: str = ""
        self._sync_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def current_file(self) -> str:
        return self._current_file

    def start_playback(self, file_path: str, hwnd: int = 0) -> bool:
        """开始播放指定电影文件
        
        :param file_path: 电影文件路径
        :param hwnd: Qt窗口句柄（可选，用于VLC嵌入）
        """
        if not os.path.exists(file_path):
            self.status_changed.emit(f"文件不存在: {file_path}")
            return False

        self._stop_internal()

        try:
            import vlc
        except ImportError:
            self.status_changed.emit("VLC库未安装，请运行: pip install python-vlc")
            log.error(TAG, "python-vlc not installed")
            return False

        try:
            self._instance = vlc.Instance("--no-xlib", "--quiet",
                                          "--no-video-title-show")
            self._player = self._instance.media_player_new()

            if hwnd:
                self._player.set_hwnd(hwnd)

            self._media = self._instance.media_new(file_path)
            self._player.set_media(self._media)
            self._media.parse()
            time.sleep(0.3)

            self._player.play()
            time.sleep(0.5)

            length = self._player.get_length()
            self._current_file = os.path.basename(file_path)
            self._playing = True
            self._paused = False
            self._running = True

            # 广播切换电影
            name_bytes = self._current_file.encode("utf-8")
            payload = bytes([CINEMA_CHANGE]) + struct.pack("!I", len(name_bytes)) + name_bytes
            self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, payload))

            # 广播播放
            self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, bytes([CINEMA_PLAY])))

            # 启动同步线程
            self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="CinemaSync")
            self._sync_thread.start()

            self.status_changed.emit(f"正在播放: {self._current_file}")
            log.log(TAG, f"Started playback: {file_path}")
            return True

        except Exception as e:
            log.error(TAG, f"Playback start error: {e}")
            self.status_changed.emit(f"播放失败: {e}")
            return False

    def toggle_pause(self) -> None:
        """切换暂停/恢复（民主：任何人都可以触发）"""
        if not self._player:
            return

        try:
            if self._paused:
                self._player.play()
                self._paused = False
                self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, bytes([CINEMA_PLAY])))
                self.status_changed.emit("已恢复播放")
            else:
                self._player.pause()
                self._paused = True
                self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, bytes([CINEMA_PAUSE])))
                self.status_changed.emit("已暂停")
        except Exception as e:
            log.error(TAG, f"Toggle pause error: {e}")

    def seek(self, position_ms: int) -> None:
        """跳转到指定位置（独裁：仅房主可操作）"""
        if not self._player:
            return
        try:
            self._player.set_time(position_ms)
            payload = bytes([CINEMA_SEEK]) + struct.pack("!q", position_ms)
            self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, payload))
            log.log(TAG, f"Seek to {position_ms}ms")
        except Exception as e:
            log.error(TAG, f"Seek error: {e}")

    def stop(self) -> None:
        """停止播放"""
        self._running = False
        self._stop_internal()
        self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, bytes([CINEMA_STOP])))
        self.status_changed.emit("观影已结束")
        log.log(TAG, "Cinema stopped")

    def _stop_internal(self) -> None:
        if self._player:
            try:
                self._player.stop()
            except Exception:
                pass
            self._player = None
        self._media = None
        if self._instance:
            try:
                self._instance.release()
            except Exception:
                pass
            self._instance = None
        self._playing = False
        self._paused = False
        self._current_file = ""

    def handle_guest_command(self, cmd: int, payload: bytes) -> None:
        """处理房客发来的电影院命令"""
        if cmd == CINEMA_PAUSE:
            if self._playing and not self._paused:
                self.toggle_pause()
        elif cmd == CINEMA_PLAY:
            if self._playing and self._paused:
                self.toggle_pause()
        elif cmd == CINEMA_SYNC_REQ:
            # 房客请求同步（中途加入等场景）
            self._send_sync()
        # CINEMA_SEEK 仅房主可操作，忽略房客发来的

    def _send_sync(self) -> None:
        """广播当前播放状态（位置+暂停/播放+文件名）"""
        if not self._player or not self._playing:
            return

        try:
            pos = self._player.get_time()
            if pos < 0:
                pos = 0
            total = self._player.get_length()
            name_bytes = self._current_file.encode("utf-8")
            state = 0x01 if self._paused else 0x00
            payload = (bytes([CINEMA_SYNC, state]) +
                       struct.pack("!qq", pos, total) +
                       struct.pack("!I", len(name_bytes)) + name_bytes)
            self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, payload))
            self.position_updated.emit(pos, total)
        except Exception as e:
            log.error(TAG, f"Sync send error: {e}")

    def _sync_loop(self) -> None:
        """定期同步循环（每5秒广播一次位置）"""
        while self._running:
            time.sleep(config.CINEMA_SYNC_INTERVAL)
            if self._running and self._playing:
                self._send_sync()

    def cleanup(self) -> None:
        self._running = False
        self._stop_internal()
