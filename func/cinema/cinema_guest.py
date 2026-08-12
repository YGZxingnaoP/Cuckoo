# -*- coding: utf-8 -*-
"""
电影院模块 —— 房客端播放控制
接收房主同步命令，使用 python-vlc + FFmpeg 字幕提取保持同步播放。
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
from func.cinema.subtitle_tool import extract_and_normalize
import config

TAG = "CinemaGuest"


class CinemaGuest(QObject):
    """房客端电影院同步播放器"""

    status_changed = Signal(str)
    position_updated = Signal(int, int)

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
        self._current_file_path: str = ""
        self._current_total: int = 0
        self._hwnd: int = 0
        self._subtitle_size: int = config.DEFAULT_SUBTITLE_SIZE
        self._sub_file: str = ""
        self._reloading: bool = False
        self._lock = threading.Lock()
        self._syncing = False
        self._last_sync_time: float = 0.0

    @property
    def is_playing(self) -> bool: return self._playing
    @property
    def is_paused(self) -> bool:  return self._paused
    @property
    def current_file(self) -> str: return self._current_file

    def set_hwnd(self, hwnd: int) -> None:
        self._hwnd = hwnd
        log.log(TAG, f"Guest hwnd set to {hwnd}")

    def has_movie(self, filename: str) -> bool:
        return os.path.isfile(os.path.join(self._movies_dir, filename))

    def get_movie_path(self, filename: str) -> str:
        return os.path.join(self._movies_dir, filename)

    def handle_host_command(self, cmd: int, payload: bytes) -> None:
        if cmd == CINEMA_PLAY:       self._do_play()
        elif cmd == CINEMA_PAUSE:    self._do_pause()
        elif cmd == CINEMA_SEEK:
            if len(payload) >= 9: self._do_seek(struct.unpack("!q", payload[1:9])[0])
        elif cmd == CINEMA_SYNC:     self._handle_sync(payload)
        elif cmd == CINEMA_STOP:     self._do_stop()
        elif cmd == CINEMA_CHANGE:   self._handle_change(payload)

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
            # 仅在 hwnd 已设置时创建 player（用户已点"加入观影"）
            if self._hwnd:
                self._start_player_internal(path)
            else:
                log.log(TAG, f"Deferred player init (hwnd not set yet): {filename}")
        except Exception as e:
            log.error(TAG, f"Handle cinema change error: {e}")

    def _handle_sync(self, payload: bytes) -> None:
        try:
            state = payload[1]
            host_paused = (state & 0x01) != 0
            host_pos, host_total = struct.unpack("!qq", payload[2:18])
            host_file = ""
            if len(payload) > 18:
                try:
                    name_len = struct.unpack("!I", payload[18:22])[0]
                    host_file = payload[22:22 + name_len].decode("utf-8")
                except Exception: pass

            if not self._player or not self._playing or (host_file and host_file != self._current_file):
                if host_file:
                    self._current_file = host_file
                    path = os.path.join(self._movies_dir, host_file)
                    if not os.path.isfile(path):
                        self.status_changed.emit(f"本地缺少电影文件: {host_file}")
                        return
                    # hwnd 未设置 = 用户还没点"加入观影" → 推迟
                    if not self._hwnd:
                        log.log(TAG, f"Sync: deferring player init, hwnd not set")
                        return
                    if not self._start_player_internal(path):
                        return
                    self._player.play()
                    time.sleep(0.2)
                    self._paused = False
                    self._syncing = True
                    self._player.set_time(host_pos)
                    self._syncing = False

            if self._reloading or not self._player or not self._playing:
                return

            if host_paused != self._paused:
                if host_paused: self._do_pause()
                else: self._do_play()

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

    def _start_player_internal(self, file_path: str) -> bool:
        if not self._hwnd:
            log.warn(TAG, "Refusing to create player: hwnd is 0")
            return False
        self._stop_player()
        try: import vlc
        except ImportError:
            self.status_changed.emit("VLC库未安装")
            return False

        try:
            self._instance = vlc.Instance("--no-xlib", "--quiet",
                "--no-video-title-show",
                f"--freetype-rel-fontsize={self._subtitle_size}")
            self._player = self._instance.media_player_new()

            if self._hwnd:
                self._player.set_hwnd(self._hwnd)

            self._current_file_path = file_path

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
        if self._player and self._playing:
            try:
                self._player.play()
                self._paused = False
                self.status_changed.emit("播放中")
            except Exception as e:
                log.error(TAG, f"Guest play error: {e}")

    def _do_pause(self) -> None:
        if self._player and self._playing:
            try:
                self._player.pause()
                self._paused = True
                self.status_changed.emit("已暂停")
            except Exception as e:
                log.error(TAG, f"Guest pause error: {e}")

    def _do_seek(self, position_ms: int) -> None:
        if self._player and self._playing:
            try:
                self._syncing = True
                self._player.set_time(position_ms)
                self._syncing = False
            except Exception as e:
                self._syncing = False
                log.error(TAG, f"Guest seek error: {e}")

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

            if was_paused: self._player.pause()
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
        if not self._player: return []
        try:
            desc = self._player.video_get_spu_description()
            return [(desc.at(i).id, desc.at(i).name.decode("utf-8", errors="replace"))
                    for i in range(desc.count)]
        except Exception: return []

    def _do_stop(self) -> None:
        self._stop_player()
        self._playing = False
        self._paused = False
        self._current_file = ""
        self.status_changed.emit("观影已结束")

    def _stop_player(self) -> None:
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

    def request_pause_resume(self) -> None:
        cmd = CINEMA_PAUSE if not self._paused else CINEMA_PLAY
        self._send(MSG_CINEMA_CMD, HOST_ID, bytes([cmd]))

    def request_sync(self) -> None:
        self._send(MSG_CINEMA_CMD, HOST_ID, bytes([CINEMA_SYNC_REQ]))

    def stop(self) -> None: self._do_stop()
    def cleanup(self) -> None: self.stop()

    def get_current_position(self) -> int:
        if self._player and self._playing:
            try: return self._player.get_time()
            except Exception: pass
        return 0

    def get_total_length(self) -> int:
        if self._player and self._playing:
            try: return self._player.get_length()
            except Exception: pass
        return self._current_total
