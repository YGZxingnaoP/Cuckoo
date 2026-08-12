# -*- coding: utf-8 -*-
"""
电影院 Tab 页面
支持电影列表、上传/发送、全屏播放、进度同步。
"""

import os
from typing import Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QSlider,
    QGroupBox, QFrame, QSplitter, QMessageBox, QMainWindow, QLayout,
    QComboBox, QApplication
)
from PySide6.QtCore import Qt, Signal, QTimer, QRect
from PySide6.QtGui import QKeyEvent, QScreen
import config


class FullscreenWindow(QMainWindow):
    """真正的全屏窗口 — 覆盖整个屏幕，按 ESC 退出"""

    closed = Signal()

    def __init__(self, video_widget: QWidget):
        super().__init__()
        self.setObjectName("cinemaFullscreen")
        self.setWindowTitle("Cuckoo 电影院 — 全屏")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("background-color: #000000;")
        # 设置到主显示器全屏
        screen = QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.availableGeometry())
        self.setCentralWidget(video_widget)
        video_widget.setStyleSheet("background-color: #000000; border: none;")

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(event)


class CinemaTab(QWidget):
    """
    电影院界面：
    - 左侧：电影文件列表
    - 右侧：播放区域（VLC嵌入）+ 控制栏
    """

    # 房主控制信号
    play_requested = Signal(str)           # 播放指定文件
    stop_requested = Signal()              # 停止播放
    toggle_pause_requested = Signal()      # 暂停/恢复
    seek_requested = Signal(int)           # 跳转(ms)
    send_movie_requested = Signal(str)     # 发送电影给所有房客
    upload_movie_requested = Signal(str)   # 上传电影到movies文件夹

    # 房客控制信号
    guest_join_requested = Signal()        # 加入观影
    guest_leave_requested = Signal()       # 离开观影
    guest_pause_requested = Signal()       # 请求暂停/恢复
    guest_sync_requested = Signal()        # 请求同步

    # 全屏切换信号（通知外部重新绑定VLC HWND）
    fullscreen_changed = Signal(bool)      # True=进入全屏, False=退出全屏

    # 字幕字号变更信号（纯本地，不涉及网络）
    subtitle_size_changed = Signal(int)    # 10-40
    subtitle_extract_requested = Signal()   # 提取字幕按钮
    spu_track_changed = Signal(int)          # 字幕轨道切换

    def __init__(self, is_host: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("cinemaTab")
        self._is_host = is_host
        self._playing = False
        self._paused = False
        self._current_total_ms = 0
        self._fullscreen = False
        self._fullscreen_window: Optional[FullscreenWindow] = None
        self._seeking = False
        self._sub_debounce_timer = QTimer(self)
        self._sub_debounce_timer.setSingleShot(True)
        self._sub_debounce_timer.setInterval(400)
        self._sub_debounce_timer.timeout.connect(self._on_sub_debounce_fire)
        self._pending_sub_size: int = config.DEFAULT_SUBTITLE_SIZE
        self._video_parent_layout: Optional[QLayout] = None
        self._video_parent_index: int = -1
        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # ═════════════════════════════════
        # 左侧：电影列表
        # ═════════════════════════════════
        left_panel = QFrame()
        left_panel.setObjectName("cinemaLeftPanel")
        left_panel.setFixedWidth(260)
        left_panel.setStyleSheet("""
            QFrame#cinemaLeftPanel {
                background-color: #0f0f0f;
                border: 1px solid #2a2a2a;
                border-radius: 8px;
            }
        """)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(6)

        # 标题
        title = QLabel("🎬 电影列表")
        title.setStyleSheet("color: #f0f0f0; font-size: 14px; font-weight: bold; background: transparent; border: none;")
        left_layout.addWidget(title)

        hint = QLabel("movies 文件夹")
        hint.setStyleSheet("color: #888; font-size: 10px; background: transparent; border: none;")
        left_layout.addWidget(hint)

        # 文件列表
        self._movie_list = QListWidget()
        self._movie_list.setStyleSheet("""
            QListWidget {
                background-color: #0a0a0a;
                border: 1px solid #2a2a2a;
                border-radius: 6px;
                color: #f0f0f0;
                outline: none;
            }
            QListWidget::item {
                padding: 8px 10px;
                border-radius: 4px;
                margin: 1px 2px;
            }
            QListWidget::item:selected { background-color: #2a2a2a; color: #f0f0f0; }
            QListWidget::item:hover { background-color: #1a1a1a; }
        """)
        self._movie_list.itemDoubleClicked.connect(self._on_movie_double_clicked)
        left_layout.addWidget(self._movie_list, stretch=1)

        # 操作按钮
        btn_frame = QFrame()
        btn_frame.setStyleSheet("background: transparent; border: none;")
        btn_layout = QVBoxLayout(btn_frame)
        btn_layout.setSpacing(4)
        btn_layout.setContentsMargins(0, 0, 0, 0)

        if self._is_host:
            self._btn_play = QPushButton("▶ 开始播放")
            self._btn_play.setMinimumHeight(36)
            self._btn_play.setStyleSheet(self._btn_primary_style())
            self._btn_play.clicked.connect(self._on_play_clicked)
            btn_layout.addWidget(self._btn_play)

            upload_row = QHBoxLayout()
            self._btn_upload = QPushButton("📤 上传电影")
            self._btn_upload.setMinimumHeight(32)
            self._btn_upload.setStyleSheet(self._btn_secondary_style())
            self._btn_upload.clicked.connect(self._on_upload_clicked)
            upload_row.addWidget(self._btn_upload)

            self._btn_send = QPushButton("📡 发送给房客")
            self._btn_send.setMinimumHeight(32)
            self._btn_send.setStyleSheet(self._btn_secondary_style())
            self._btn_send.clicked.connect(self._on_send_clicked)
            upload_row.addWidget(self._btn_send)
            btn_layout.addLayout(upload_row)

            self._btn_refresh = QPushButton("🔄 刷新列表")
            self._btn_refresh.setMinimumHeight(28)
            self._btn_refresh.setStyleSheet(self._btn_minor_style())
            self._btn_refresh.clicked.connect(self._on_refresh_clicked)
            btn_layout.addWidget(self._btn_refresh)
        else:
            self._btn_join = QPushButton("🔗 加入观影")
            self._btn_join.setMinimumHeight(36)
            self._btn_join.setStyleSheet(self._btn_primary_style())
            self._btn_join.clicked.connect(self._on_join_clicked)
            btn_layout.addWidget(self._btn_join)

            self._btn_refresh = QPushButton("🔄 刷新列表")
            self._btn_refresh.setMinimumHeight(28)
            self._btn_refresh.setStyleSheet(self._btn_minor_style())
            self._btn_refresh.clicked.connect(self._on_refresh_clicked)
            btn_layout.addWidget(self._btn_refresh)

        left_layout.addWidget(btn_frame)

        main_layout.addWidget(left_panel)

        # ═════════════════════════════════════
        # 右侧：播放区域 + 控制栏
        # ═════════════════════════════════════
        right_panel = QFrame()
        right_panel.setObjectName("cinemaRightPanel")
        right_panel.setStyleSheet("""
            QFrame#cinemaRightPanel {
                background-color: #0a0a0a;
                border: 1px solid #2a2a2a;
                border-radius: 8px;
            }
        """)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(6)

        # 播放区域
        self._video_container = QFrame()
        self._video_container.setObjectName("videoContainer")
        self._video_container.setMinimumSize(480, 270)
        self._video_container.setStyleSheet(
            "background-color: #000000; border: 1px solid #2a2a2a; border-radius: 4px;"
        )
        self._video_label = QLabel("等待播放...")
        self._video_label.setAlignment(Qt.AlignCenter)
        self._video_label.setStyleSheet(
            "color: #666; font-size: 16px; background: transparent; border: none;"
        )
        video_layout = QVBoxLayout(self._video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(self._video_label)
        # stretch=10 确保视频容器拿满空间，状态栏/进度条/按钮/字幕设置被挤压到最小
        right_layout.addWidget(self._video_container, stretch=10)

        # 状态栏
        self._status_label = QLabel("就绪")
        self._status_label.setStyleSheet(
            "color: #888; font-size: 12px; background: transparent; border: none; padding: 2px 4px;"
        )
        right_layout.addWidget(self._status_label)

        # 进度条
        progress_row = QHBoxLayout()
        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setStyleSheet("color: #aaa; font-size: 11px; background: transparent; border: none;")
        self._time_label.setFixedWidth(120)
        progress_row.addWidget(self._time_label)

        self._progress_slider = QSlider(Qt.Horizontal)
        self._progress_slider.setRange(0, 1000)
        self._progress_slider.setValue(0)
        self._progress_slider.setEnabled(self._is_host)  # 仅房主可拖动
        self._progress_slider.sliderPressed.connect(self._on_slider_pressed)
        self._progress_slider.sliderReleased.connect(self._on_slider_released)
        self._progress_slider.sliderMoved.connect(self._on_slider_moved)
        progress_row.addWidget(self._progress_slider, stretch=1)

        right_layout.addLayout(progress_row)

        # 控制按钮栏
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)

        self._btn_pause = QPushButton("⏯ 暂停")
        self._btn_pause.setMinimumHeight(32)
        self._btn_pause.setStyleSheet(self._btn_secondary_style())
        self._btn_pause.clicked.connect(self._on_pause_clicked)
        self._btn_pause.setEnabled(False)
        ctrl_row.addWidget(self._btn_pause)

        self._btn_stop = QPushButton("⏹ 退出观影")
        self._btn_stop.setMinimumHeight(32)
        self._btn_stop.setStyleSheet(self._btn_danger_style())
        self._btn_stop.clicked.connect(self._on_stop_clicked)
        self._btn_stop.setEnabled(False)
        ctrl_row.addWidget(self._btn_stop)

        self._btn_fullscreen = QPushButton("⛶ 全屏")
        self._btn_fullscreen.setMinimumHeight(32)
        self._btn_fullscreen.setStyleSheet(self._btn_minor_style())
        self._btn_fullscreen.clicked.connect(self._on_fullscreen_clicked)
        ctrl_row.addWidget(self._btn_fullscreen)

        ctrl_row.addStretch()
        right_layout.addLayout(ctrl_row)

        # ── 字幕设置 QGroupBox ──
        sub_group = QGroupBox("字幕设置")
        sub_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                color: #ccc;
                border: 1px solid #2a2a2a;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 14px;
                background-color: #0f0f0f;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        sub_layout = QVBoxLayout(sub_group)
        sub_layout.setSpacing(4)

        # 语言选择行
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel("语言:"))
        self._spu_combo = QComboBox()
        self._spu_combo.setMinimumWidth(200)
        self._spu_combo.setStyleSheet("""
            QComboBox {
                background-color: #141414; border: 1px solid #2a2a2a;
                border-radius: 4px; padding: 2px 6px; color: #f0f0f0;
            }
            QComboBox::drop-down { border: none; width: 20px; }
            QComboBox QAbstractItemView {
                background-color: #141414; border: 1px solid #2a2a2a;
                selection-background-color: #2a2a2a; color: #f0f0f0;
            }
        """)
        self._spu_combo.currentIndexChanged.connect(self._on_spu_track_changed)
        lang_row.addWidget(self._spu_combo, stretch=1)
        sub_layout.addLayout(lang_row)

        # 字号 + 提取按钮行
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("大小:"))
        self._subtitle_slider = QSlider(Qt.Horizontal)
        self._subtitle_slider.setRange(10, 40)
        self._subtitle_slider.setValue(config.DEFAULT_SUBTITLE_SIZE)
        self._subtitle_slider.setTickPosition(QSlider.TicksBelow)
        self._subtitle_slider.setTickInterval(5)
        self._subtitle_slider.setFixedWidth(160)
        self._subtitle_slider.valueChanged.connect(self._on_subtitle_size_changed)
        self._subtitle_label = QLabel(str(config.DEFAULT_SUBTITLE_SIZE))
        self._subtitle_label.setFixedWidth(24)
        self._subtitle_label.setStyleSheet("color: #aaa; font-size: 11px; background: transparent; border: none;")
        size_row.addWidget(self._subtitle_slider)
        size_row.addWidget(self._subtitle_label)
        self._btn_extract_sub = QPushButton("📝 提取ASS")
        self._btn_extract_sub.setMinimumHeight(28)
        self._btn_extract_sub.setStyleSheet(self._btn_minor_style())
        self._btn_extract_sub.setToolTip("从MKV提取ASS字幕并缩放匹配当前字号")
        self._btn_extract_sub.clicked.connect(self._on_extract_sub_clicked)
        size_row.addWidget(self._btn_extract_sub)
        size_row.addStretch()
        sub_layout.addLayout(size_row)

        right_layout.addWidget(sub_group)

        main_layout.addWidget(right_panel, stretch=1)

        # 启动定时器刷新进度条
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(500)
        self._progress_timer.timeout.connect(self._on_progress_tick)
        self._progress_timer.start()

        # 初始加载电影列表
        self.refresh_movie_list()

    # ═════════════════════════════════════
    # 公共接口
    # ═════════════════════════════════════

    def refresh_movie_list(self) -> None:
        """刷新movies文件夹下的电影列表"""
        self._movie_list.clear()
        os.makedirs(config.MOVIES_DIR, exist_ok=True)
        try:
            files = sorted(os.listdir(config.MOVIES_DIR))
            for f in files:
                fpath = os.path.join(config.MOVIES_DIR, f)
                if os.path.isfile(fpath):
                    ext = os.path.splitext(f)[1].lower()
                    # 支持的视频格式
                    if ext in ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.wmv',
                               '.m4v', '.mpg', '.mpeg', '.ts', '.ogv', '.3gp'):
                        size_mb = os.path.getsize(fpath) / (1024 * 1024)
                        item = QListWidgetItem(f"{f}\n({size_mb:.1f} MB)")
                        item.setData(Qt.UserRole, f)
                        self._movie_list.addItem(item)
        except Exception as e:
            self._status_label.setText(f"读取电影列表失败: {e}")

    def get_selected_movie(self) -> str:
        """获取选中的电影文件名，返回完整路径"""
        item = self._movie_list.currentItem()
        if item:
            filename = item.data(Qt.UserRole)
            return os.path.join(config.MOVIES_DIR, filename)
        return ""

    def set_playing_state(self, playing: bool, paused: bool = False) -> None:
        """设置播放状态"""
        self._playing = playing
        self._paused = paused
        self._btn_pause.setEnabled(playing)
        self._btn_stop.setEnabled(playing)
        self._progress_slider.setEnabled(playing and self._is_host)

        if playing and not paused:
            self._btn_pause.setText("⏯ 暂停")
        elif playing and paused:
            self._btn_pause.setText("▶ 恢复")

    def set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def update_position(self, current_ms: int, total_ms: int) -> None:
        """更新进度条位置（由同步/定时器驱动）"""
        if self._seeking:
            return  # 用户正在拖动，不覆盖
        self._current_total_ms = total_ms
        if total_ms > 0:
            val = int(current_ms * 1000 / total_ms)
            self._progress_slider.blockSignals(True)
            self._progress_slider.setValue(min(val, 1000))
            self._progress_slider.blockSignals(False)
        self._time_label.setText(
            f"{self._format_time(current_ms)} / {self._format_time(total_ms)}"
        )

    def get_video_container_widget(self) -> QWidget:
        """返回视频容器 QWidget（供VLC绑定窗口句柄）"""
        return self._video_container

    def set_video_container_win_id(self, win_id: int) -> None:
        """设置VLC输出窗口（VLC需绑定到具体HWND）"""
        # VLC需要int类型的窗口句柄
        pass  # 将在main_window中设置

    # ═════════════════════════════════════
    # 事件处理
    # ═════════════════════════════════════

    def _on_play_clicked(self) -> None:
        path = self.get_selected_movie()
        if not path:
            QMessageBox.information(self, "提示", "请先选择一个电影文件")
            return
        if not os.path.exists(path):
            QMessageBox.warning(self, "错误", f"文件不存在: {path}")
            return
        self.play_requested.emit(path)

    def _on_upload_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择电影文件", "",
            "视频文件 (*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv *.m4v *.mpg *.mpeg *.ts *.ogv *.3gp);;所有文件 (*.*)"
        )
        if path:
            self.upload_movie_requested.emit(path)

    def _on_send_clicked(self) -> None:
        path = self.get_selected_movie()
        if not path:
            QMessageBox.information(self, "提示", "请先选择一个电影文件发送")
            return
        self.send_movie_requested.emit(path)

    def _on_refresh_clicked(self) -> None:
        self.refresh_movie_list()

    def _on_join_clicked(self) -> None:
        self.guest_join_requested.emit()

    def _on_pause_clicked(self) -> None:
        if self._is_host:
            self.toggle_pause_requested.emit()
        else:
            self.guest_pause_requested.emit()

    def _on_stop_clicked(self) -> None:
        if self._is_host:
            self.stop_requested.emit()
        else:
            self.guest_leave_requested.emit()

    def _on_fullscreen_clicked(self) -> None:
        """切换全屏"""
        if self._fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _on_subtitle_size_changed(self, value: int) -> None:
        self._subtitle_label.setText(str(value))
        self._pending_sub_size = value
        self._sub_debounce_timer.start()  # 重启计时器，400ms 内连续拖动不触发

    def _on_sub_debounce_fire(self) -> None:
        """滑条停止拖动 400ms 后才真正触发字幕重建"""
        self.subtitle_size_changed.emit(self._pending_sub_size)

    def _on_extract_sub_clicked(self) -> None:
        self.subtitle_extract_requested.emit()

    def _on_spu_track_changed(self, index: int) -> None:
        track_id = self._spu_combo.currentData()
        if track_id is not None:
            self.spu_track_changed.emit(track_id)

    def set_spu_tracks(self, tracks: list[tuple[int, str]]) -> None:
        """填充字幕轨道下拉框"""
        current = self._spu_combo.currentData()
        self._spu_combo.blockSignals(True)
        self._spu_combo.clear()
        for tid, name in tracks:
            self._spu_combo.addItem(name, tid)
        # 恢复选中
        for i in range(self._spu_combo.count()):
            if self._spu_combo.itemData(i) == current:
                self._spu_combo.setCurrentIndex(i)
                break
        self._spu_combo.blockSignals(False)

    def _enter_fullscreen(self) -> None:
        """进入全屏：把视频容器提升到独立全屏窗口"""
        if self._fullscreen:
            return

        # 1. 保存原布局位置和 stretch factor
        parent_layout = None
        parent_index = -1
        self._saved_stretch = 10
        parent_widget = self._video_container.parentWidget()
        if parent_widget and parent_widget.layout():
            parent_layout = parent_widget.layout()
            for i in range(parent_layout.count()):
                item = parent_layout.itemAt(i)
                if item.widget() is self._video_container:
                    parent_index = i
                    self._saved_stretch = parent_layout.stretch(i)
                    break

        self._video_parent_layout = parent_layout
        self._video_parent_index = parent_index

        # 2. 从原布局中移除
        if parent_layout and parent_index >= 0:
            parent_layout.removeWidget(self._video_container)

        # 3. 创建全屏窗口，把视频容器嵌入
        self._video_container.setParent(None)  # 解除原父子关系
        self._fullscreen_window = FullscreenWindow(self._video_container)
        self._fullscreen_window.closed.connect(self._exit_fullscreen)
        self._fullscreen_window.showFullScreen()

        self._fullscreen = True
        self._btn_fullscreen.setText("⛶ 退出全屏")
        self._status_label.setText("全屏模式 — 按 Esc 退出")
        self._video_container.winId()  # 强制创建原生窗口
        self.fullscreen_changed.emit(True)

    def _exit_fullscreen(self) -> None:
        """退出全屏：把视频容器放回原位"""
        if not self._fullscreen:
            return

        # 1. 先隐藏全屏窗口避免闪烁，再取出视频容器
        fw = self._fullscreen_window
        self._fullscreen_window = None
        if fw:
            fw.hide()
            fw.takeCentralWidget()
            fw.close()
            fw.deleteLater()

        # 2. 把视频容器放回原布局，恢复 stretch
        self._video_container.setParent(None)
        if self._video_parent_layout and self._video_parent_index >= 0:
            self._video_parent_layout.insertWidget(self._video_parent_index, self._video_container,
                                                    stretch=getattr(self, '_saved_stretch', 10))
        self._video_container.setStyleSheet(
            "background-color: #000000; border: 1px solid #2a2a2a; border-radius: 4px;"
        )
        self._video_container.show()

        # 3. 强制布局刷新，修复退出全屏后位置偏移
        if self._video_parent_layout:
            self._video_parent_layout.activate()
            self._video_parent_layout.update()
        QTimer.singleShot(50, self._video_container.updateGeometry)

        self._fullscreen = False
        self._btn_fullscreen.setText("⛶ 全屏")
        self._status_label.setText("")
        self._video_container.winId()  # 强制创建原生窗口
        self.fullscreen_changed.emit(False)

    @property
    def is_fullscreen(self) -> bool:
        return self._fullscreen

    def toggle_fullscreen(self) -> None:
        """切换全屏状态"""
        self._on_fullscreen_clicked()

    def _on_slider_pressed(self) -> None:
        self._seeking = True

    def _on_slider_released(self) -> None:
        self._seeking = False
        if self._current_total_ms > 0:
            pos = int(self._progress_slider.value() * self._current_total_ms / 1000)
            self.seek_requested.emit(pos)

    def _on_slider_moved(self, value: int) -> None:
        if self._current_total_ms > 0:
            pos = int(value * self._current_total_ms / 1000)
            self._time_label.setText(
                f"{self._format_time(pos)} / {self._format_time(self._current_total_ms)}"
            )

    def _on_movie_double_clicked(self, item: QListWidgetItem) -> None:
        if self._is_host:
            path = os.path.join(config.MOVIES_DIR, item.data(Qt.UserRole))
            if os.path.exists(path):
                self.play_requested.emit(path)

    def _on_progress_tick(self) -> None:
        """定时器触发：通知外部更新进度"""
        # 实际进度由外部通过 update_position() 更新
        pass

    @property
    def is_fullscreen(self) -> bool:
        return self._fullscreen

    def toggle_fullscreen(self) -> None:
        """切换全屏状态"""
        self._on_fullscreen_clicked()

    # ═════════════════════════════════════
    # 样式
    # ═════════════════════════════════════

    @staticmethod
    def _btn_primary_style() -> str:
        return """
            QPushButton {
                background-color: #f0f0f0; color: #0a0a0a; font-weight: bold;
                border: none; border-radius: 6px; padding: 6px 12px;
            }
            QPushButton:hover { background-color: #ffffff; }
            QPushButton:pressed { background-color: #cccccc; }
            QPushButton:disabled { background-color: #2a2a2a; color: #555; }
        """

    @staticmethod
    def _btn_secondary_style() -> str:
        return """
            QPushButton {
                background-color: #1a1a1a; color: #f0f0f0; font-weight: bold;
                border: 1px solid #2a2a2a; border-radius: 6px; padding: 4px 10px;
            }
            QPushButton:hover { background-color: #2a2a2a; border-color: #3a3a3a; }
            QPushButton:pressed { background-color: #141414; }
            QPushButton:disabled { background-color: #141414; color: #555; border-color: #1a1a1a; }
        """

    @staticmethod
    def _btn_minor_style() -> str:
        return """
            QPushButton {
                background-color: transparent; color: #888; font-weight: normal;
                border: 1px solid #2a2a2a; border-radius: 4px; padding: 3px 8px;
            }
            QPushButton:hover { color: #f0f0f0; border-color: #888; }
            QPushButton:disabled { color: #444; border-color: #1a1a1a; }
        """

    @staticmethod
    def _btn_danger_style() -> str:
        return """
            QPushButton {
                background-color: #2a1515; color: #f44; font-weight: bold;
                border: 1px solid #3a1a1a; border-radius: 6px; padding: 4px 10px;
            }
            QPushButton:hover { background-color: #3a1a1a; color: #f66; }
            QPushButton:pressed { background-color: #1a0a0a; }
            QPushButton:disabled { background-color: #141414; color: #555; border-color: #1a1a1a; }
        """

    @staticmethod
    def _format_time(ms: int) -> str:
        if ms < 0:
            ms = 0
        s = ms // 1000
        m, sec = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"
