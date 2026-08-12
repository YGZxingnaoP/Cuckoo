# -*- coding: utf-8 -*-
"""
电影院模块 —— 房主端播放控制
使用 python-vlc + FFmpeg 字幕提取，广播播放/暂停/跳转/同步命令。
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
from func.cinema.subtitle_tool import extract_and_normalize
import config

TAG = "CinemaHost"


class CinemaHost(QObject):
    """房主端电影院控制器"""

    status_changed = Signal(str)
    position_updated = Signal(int, int)

    def __init__(self, broadcast_callback: Callable):
        super().__init__()
        self._broadcast = broadcast_callback
        self._player: Optional[object] = None
        self._instance: Optional[object] = None
        self._media: Optional[object] = None
        self._playing = False
        self._paused = False
        self._current_file: str = ""
        self._current_file_path: str = ""
        self._hwnd: int = 0
        self._subtitle_size: int = config.DEFAULT_SUBTITLE_SIZE
        self._sub_file: str = ""       # 外挂字幕路径
        self._reloading: bool = False
        self._sync_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

    @property
    def is_playing(self) -> bool: return self._playing
    @property
    def is_paused(self) -> bool:  return self._paused
    @property
    def current_file(self) -> str: return self._current_file

    # ═════════════════════════════════════════
    # 播放控制
    # ═════════════════════════════════════════
    def start_playback(self, file_path: str, hwnd: int = 0) -> bool:
        if not os.path.exists(file_path):
            self.status_changed.emit(f"文件不存在: {file_path}")
            return False

        self._stop_internal()

        try:
            import vlc
        except ImportError:
            self.status_changed.emit("VLC库未安装")
            return False

        try:
            self._instance = vlc.Instance("--no-xlib", "--quiet",
                "--no-video-title-show",
                f"--freetype-rel-fontsize={self._subtitle_size}")
            self._player = self._instance.media_player_new()

            if hwnd:
                self._hwnd = hwnd
                self._player.set_hwnd(hwnd)

            self._current_file_path = file_path
            self._current_file = os.path.basename(file_path)

            # ── 字幕：FFmpeg 提取 + 归一化 → 外挂 ──
            self._sub_file = extract_and_normalize(file_path, self._subtitle_size) or ""

            if self._sub_file and os.path.isfile(self._sub_file):
                self._media = self._instance.media_new(file_path,
                    f":sub-file={self._sub_file}")
                log.log(TAG, f"Loaded external sub: {self._sub_file}")
            else:
                self._media = self._instance.media_new(file_path)

            self._player.set_media(self._media)
            self._media.parse()
            time.sleep(0.3)

            self._player.play()
            time.sleep(0.5)

            self._playing = True
            self._paused = False
            self._running = True

            name_bytes = self._current_file.encode("utf-8")
            payload = bytes([CINEMA_CHANGE]) + struct.pack("!I", len(name_bytes)) + name_bytes
            self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, payload))
            self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, bytes([CINEMA_PLAY])))

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
        if self._reloading or not self._player:
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

    def set_subtitle_size(self, size: int, force: bool = False) -> None:
        if not self._player or not self._playing: return
        if size == self._subtitle_size and not force: return

        self._subtitle_size = size
        self._reloading = True
        log.log(TAG, f"Subtitle size → {size}" + (" (force)" if force else ""))

        try:
            was_paused = self._paused
            pos = self._player.get_time()
            if pos < 0: pos = 0
            active_spu = self._player.video_get_spu()

            # 重新提取 + 归一化字幕
            self._sub_file = extract_and_normalize(self._current_file_path, size) or ""

            self._player.stop()
            self._instance.release()
            self._instance = None

            import vlc
            self._instance = vlc.Instance("--no-xlib", "--quiet",
                "--no-video-title-show",
                f"--freetype-rel-fontsize={size}")
            self._player = self._instance.media_player_new()
            if self._hwnd:
                self._player.set_hwnd(self._hwnd)

            if self._sub_file and os.path.isfile(self._sub_file):
                self._media = self._instance.media_new(self._current_file_path,
                    f":sub-file={self._sub_file}")
            else:
                self._media = self._instance.media_new(self._current_file_path)

            self._player.set_media(self._media)
            self._media.parse()
            time.sleep(0.2)

            self._player.play()
            time.sleep(0.5)
            if active_spu >= 0:
                self._player.video_set_spu(active_spu)
            self._player.set_time(pos)

            if was_paused:
                self._player.pause()

            self.status_changed.emit(f"字幕大小: {size}")
        except Exception as e:
            log.error(TAG, f"Subtitle resize error: {e}")
        finally:
            self._reloading = False

    def set_subtitle_track(self, track_id: int) -> None:
        if self._player and self._playing:
            try:
                self._player.video_set_spu(track_id)
                log.log(TAG, f"SPU track → {track_id}")
            except Exception as e:
                log.error(TAG, f"Set SPU error: {e}")

    def get_spu_tracks(self) -> list[tuple[int, str]]:
        if not self._player:
            return []
        try:
            desc = self._player.video_get_spu_description()
            return [(desc.at(i).id, desc.at(i).name.decode("utf-8", errors="replace"))
                    for i in range(desc.count)]
        except Exception:
            return []

    def seek(self, position_ms: int) -> None:
        if not self._player: return
        try:
            self._player.set_time(position_ms)
            self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID,
                bytes([CINEMA_SEEK]) + struct.pack("!q", position_ms)))
            log.log(TAG, f"Seek to {position_ms}ms")
        except Exception as e:
            log.error(TAG, f"Seek error: {e}")

    def stop(self) -> None:
        self._running = False
        self._stop_internal()
        self._broadcast(build_frame(MSG_CINEMA_CMD, HOST_ID, BROADCAST_ID, bytes([CINEMA_STOP])))
        self.status_changed.emit("观影已结束")
        log.log(TAG, "Cinema stopped")

    def _stop_internal(self) -> None:
        self._sub_file = ""
        if self._player:
            try: self._player.stop()
            except Exception: pass
            self._player = None
        self._media = None
        if self._instance:
            try: self._instance.release()
            except Exception: pass
            self._instance = None
        self._playing = False
        self._paused = False
        self._current_file = ""
        self._current_file_path = ""

    # ═════════════════════════════════════════
    # 网络
    # ═════════════════════════════════════════
    def handle_guest_command(self, cmd: int, payload: bytes) -> None:
        if cmd == CINEMA_PAUSE and self._playing and not self._paused:
            self.toggle_pause()
        elif cmd == CINEMA_PLAY and self._playing and self._paused:
            self.toggle_pause()
        elif cmd == CINEMA_SYNC_REQ:
            self._send_sync()

    def _send_sync(self) -> None:
        if self._reloading or not self._player or not self._playing:
            return
        try:
            pos = self._player.get_time()
            if pos < 0: pos = 0
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
        while self._running:
            time.sleep(config.CINEMA_SYNC_INTERVAL)
            if self._running and self._playing:
                self._send_sync()

    def cleanup(self) -> None:
        self._running = False
        self._stop_internal()
