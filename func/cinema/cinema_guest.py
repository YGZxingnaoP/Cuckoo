# -*- coding: utf-8 -*-
"""
电影院模块 —— 房客端播放控制
接收房主同步命令，使用 python-vlc 保持同步播放。
"""

import os
import struct
import threading
import time
from typing import Optional, Callable

from PySide6.QtCore import QObject, Signal

from common import logger as log
from core.protocol import (
    build_frame, HOST_ID,
    MSG_CINEMA_CMD, CINEMA_PLAY, CINEMA_PAUSE, CINEMA_SEEK,
    CINEMA_SYNC, CINEMA_STOP, CINEMA_CHANGE, CINEMA_SYNC_REQ
)
import config

TAG = "CinemaGuest"


class CinemaGuest(QObject):
    """房客端电影院同步播放器"""

    status_changed = Signal(str)
    position_updated = Signal(int, int)  # current_ms, total_ms

    def __init__(self, send_callback: Callable, movies_dir: str = None):
        super().__init__()
        self._send = send_callback
        self._movies_dir = movies_dir or config.MOVIES_DIR
        self._player: Optional[object] = None
        self._instance: Optional[object] = None
        self._media: Optional[object] = None
        self._playing = False
        self._paused = False
        self._current_file: str = ""
        self._current_total: int = 0
        self._hwnd: int = 0  # ★ Qt 视频容器窗口句柄
        self._lock = threading.Lock()
        self._syncing = False
        self._last_sync_time: float = 0.0

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def current_file(self) -> str:
        return self._current_file

    def set_hwnd(self, hwnd: int) -> None:
        """设置 VLC 要嵌入的 Qt 窗口句柄（必须在播放前调用）"""
        self._hwnd = hwnd

    def has_movie(self, filename: str) -> bool:
        path = os.path.join(self._movies_dir, filename)
        return os.path.isfile(path)

    def get_movie_path(self, filename: str) -> str:
        return os.path.join(self._movies_dir, filename)

    # ═════════════════════════════════════════
    # 网络命令入口
    # ═════════════════════════════════════════
    def handle_host_command(self, cmd: int, payload: bytes) -> None:
        if cmd == CINEMA_PLAY:
            self._do_play()
        elif cmd == CINEMA_PAUSE:
            self._do_pause()
        elif cmd == CINEMA_SEEK:
            if len(payload) >= 9:
                pos = struct.unpack("!q", payload[1:9])[0]
                self._do_seek(pos)
        elif cmd == CINEMA_SYNC:
            self._handle_sync(payload)
        elif cmd == CINEMA_STOP:
            self._do_stop()
        elif cmd == CINEMA_CHANGE:
            self._handle_change(payload)

    # ═════════════════════════════════════════
    # 命令处理
    # ═════════════════════════════════════════
    def _handle_change(self, payload: bytes) -> None:
        try:
            name_len = struct.unpack("!I", payload[1:5])[0]
            filename = payload[5:5 + name_len].decode("utf-8")
            self._current_file = filename
            path = os.path.join(self._movies_dir, filename)
            if not os.path.isfile(path):
                self.status_changed.emit(f"本地缺少电影文件: {filename}")
                self._playing = False
                return
            self._start_player_internal(path)
        except Exception as e:
            log.error(TAG, f"Handle cinema change error: {e}")

    def _handle_sync(self, payload: bytes) -> None:
        """
        处理房主同步广播。
        这是核心入口 — 即使房客还没创建播放器，同步数据也会驱动初始化。
        """
        try:
            state = payload[1]
            host_paused = (state & 0x01) != 0
            host_pos, host_total = struct.unpack("!qq", payload[2:18])

            # 提取文件名
            host_file = ""
            if len(payload) > 18:
                try:
                    name_len = struct.unpack("!I", payload[18:22])[0]
                    host_file = payload[22:22 + name_len].decode("utf-8")
                except Exception:
                    pass

            # 如果还没播放或文件变了 → 先创建/切换播放器
            if not self._player or not self._playing or (host_file and host_file != self._current_file):
                if host_file:
                    self._current_file = host_file
                    path = os.path.join(self._movies_dir, host_file)
                    if not os.path.isfile(path):
                        self.status_changed.emit(f"本地缺少电影文件: {host_file}")
                        return
                    if not self._start_player_internal(path):
                        return
                    self._player.play()
                    time.sleep(0.2)
                    self._paused = False
                    self._syncing = True
                    self._player.set_time(host_pos)
                    self._syncing = False

            if not self._player or not self._playing:
                return

            # 同步暂停/播放状态
            if host_paused != self._paused:
                if host_paused:
                    self._do_pause()
                else:
                    self._do_play()

            # 偏差微调
            local_pos = self._player.get_time()
            diff = abs(host_pos - local_pos)
            if diff > config.CINEMA_SYNC_THRESHOLD * 1000:
                self._syncing = True
                self._player.set_time(host_pos)
                self._syncing = False
                log.log(TAG, f"Sync corrected: {local_pos}ms → {host_pos}ms (diff={diff}ms)")

            self._current_total = host_total
            self.position_updated.emit(host_pos, host_total)
            self._last_sync_time = time.time()

        except Exception as e:
            log.error(TAG, f"Sync handle error: {e}")

    # ═════════════════════════════════════════
    # VLC 播放器生命周期
    # ═════════════════════════════════════════
    def _start_player_internal(self, file_path: str) -> bool:
        """创建 VLC 播放器并加载文件。返回是否成功。"""
        self._stop_player()

        try:
            import vlc
        except ImportError:
            self.status_changed.emit("VLC库未安装")
            return False

        try:
            self._instance = vlc.Instance("--no-xlib", "--quiet",
                                          "--no-video-title-show")
            self._player = self._instance.media_player_new()

            # ★ set_hwnd 必须在 set_media/play 之前，否则 VLC 自建窗口
            if self._hwnd:
                self._player.set_hwnd(self._hwnd)

            self._media = self._instance.media_new(file_path)
            self._player.set_media(self._media)
            self._media.parse()
            time.sleep(0.3)

            self._current_total = self._player.get_length()
            self._playing = True
            self._paused = True

            self.status_changed.emit(f"已加载: {self._current_file}")
            log.log(TAG, f"Guest player ready (hwnd={self._hwnd}): {file_path}")
            return True

        except Exception as e:
            log.error(TAG, f"Guest player start error: {e}")
            self.status_changed.emit(f"播放器启动失败: {e}")
            return False

    def _do_play(self) -> None:
        if not self._player or not self._playing:
            return
        try:
            self._player.play()
            self._paused = False
            self.status_changed.emit("播放中")
        except Exception as e:
            log.error(TAG, f"Guest play error: {e}")

    def _do_pause(self) -> None:
        if not self._player or not self._playing:
            return
        try:
            self._player.pause()
            self._paused = True
            self.status_changed.emit("已暂停")
        except Exception as e:
            log.error(TAG, f"Guest pause error: {e}")

    def _do_seek(self, position_ms: int) -> None:
        if not self._player or not self._playing:
            return
        try:
            self._syncing = True
            self._player.set_time(position_ms)
            self._syncing = False
        except Exception as e:
            self._syncing = False
            log.error(TAG, f"Guest seek error: {e}")

    def _do_stop(self) -> None:
        self._stop_player()
        self._playing = False
        self._paused = False
        self._current_file = ""
        self.status_changed.emit("观影已结束")

    def _stop_player(self) -> None:
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

    # ═════════════════════════════════════════
    # 对外方法
    # ═════════════════════════════════════════
    def request_pause_resume(self) -> None:
        cmd = CINEMA_PAUSE if not self._paused else CINEMA_PLAY
        self._send(MSG_CINEMA_CMD, HOST_ID, bytes([cmd]))

    def request_sync(self) -> None:
        self._send(MSG_CINEMA_CMD, HOST_ID, bytes([CINEMA_SYNC_REQ]))

    def stop(self) -> None:
        self._do_stop()

    def cleanup(self) -> None:
        self.stop()

    def get_current_position(self) -> int:
        if self._player and self._playing:
            try:
                return self._player.get_time()
            except Exception:
                pass
        return 0

    def get_total_length(self) -> int:
        if self._player and self._playing:
            try:
                return self._player.get_length()
            except Exception:
                pass
        return self._current_total
