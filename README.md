# Cuckoo 私有实时通信平台

```
衔丝鸟
不栖于云，不鸣于野，只在窄窄的线缆间衔起零落的丝缕
为你衔来远方的信号
```

<div align="center">
  <img src="icon.png" alt="Cuckoo Icon" width="256" />
</div>

供 10 人以下私密使用的轻量级局域网通信工具，完全脱离公网服务器依赖，基于 Radmin VPN 虚拟局域网构建。

---

## 运行环境

### 系统要求
- **操作系统**：Windows 10 / 11 x64（需 DirectX 11+ 显卡）
- **网络环境**：所有参与者需安装 **Radmin VPN** 并连接至同一虚拟网络，获得可达的虚拟 IP（如 `10.x.x.x`）。
- **防火墙设置**：需放行 `python.exe`（开发模式）或 `Cuckoo.exe`（打包模式）的 TCP 入站与出站规则。
- **音频设备**：麦克风与扬声器/耳机（语音通话必需，建议佩戴耳机以避免回声）。

### Python 环境
项目内置了嵌入式 Python 运行时（位于 `runtime/` 目录），**无需在系统中安装 Python**。

核心依赖：
- **PySide6** — 跨平台 UI 框架
- **dxcam** — DirectX 屏幕采集（替代 GDI，3~5 倍性能提升）
- **turbojpeg** — libjpeg-turbo SIMD JPEG 编码（替代 OpenCV 编码，2~3 倍性能提升）
- **OpenCV** — 图像缩放
- **numpy** — 音频/图像向量化计算
- **pyaudio** — 音频采集与播放
- **soundcard** — WASAPI Loopback 系统声音采集
- **python-vlc** — VLC 视频播放引擎（影院功能）

---

## 核心功能

| 功能 | 描述 |
|------|------|
| **投屏共享** | 房主实时采集屏幕广播给房客，支持 720p/1080p/原画质，15/30/60/120 FPS 动态切换 |
| **语音通话** | 全双工多人语音，独立 TCP 通道，专业音频管道（噪声门 + AGC + 抖动缓冲 + 软限幅） |
| **文件传输** | 任意文件/文件夹传输，保留目录结构。断点续传 + 接收确认弹窗 |
| **文字聊天** | 多人实时聊天，显示昵称与时间戳 |
| **电影院** | 房主播放 MP4/MKV 等视频，房客加入观影，实时进度同步，支持全屏 |

---

## 技术亮点

### 投屏 — DirectX + libjpeg-turbo
- **dxcam**（DirectX Graphics Capture）截屏，比传统 GDI（mss）快 3~5 倍
- **turbojpeg**（libjpeg-turbo SIMD）JPEG 编码，比 OpenCV 快 2~3 倍
- 接收端独立解码线程 + 智能丢帧，延迟极低

### 语音 — 专业音频管道
- 动态包络噪声门平滑压制环境底噪
- 抖动缓冲区消除网络电音
- AGC 自动增益 + Tanh 软限幅，极限增幅不破音
- MCU 服务端混音，排除自身回声后分发

### 文件 — 可靠传输与断点续传
- Chunk 序号+ACK 确认+丢失检测+自动重传
- 发送方与接收方双向 JSON 状态持久化
- 非空文件 MD5 完整性校验，通过才最终落盘
- **接收确认弹窗**：发送前弹出文件名/大小，接收方可选择接受或拒绝

### 电影院 — 同步观影
- python-vlc 引擎 + 字幕支持，VLC 运行时完全打包进 EXE
- 房主广播播放/暂停/跳转/同步命令
- 5 秒一次位置同步广播，偏差 >100ms 自动校正
- 暂停民主制（任何人可暂停），进度条独裁制（仅房主可拖拽）

---

## 使用指南

### 启动
```bash
# 开发模式
.\runtime\python.exe main.py

# 打包模式
Cuckoo.exe
```

### 角色选择
- **房主（Host）**：输入昵称，点击确认创建房间
- **房客（Guest）**：输入昵称 + 房主 IP，点击加入房间

### 各 Tab 操作

| Tab | 操作 |
|-----|------|
| **投屏** | 房主点击「开始投屏」，可调分辨率/帧率。房客端自动渲染 |
| **语音** | 点击「开启麦克风」，拖动滑块调节音量增幅 |
| **文件** | 选择目标用户 → 选文件/文件夹 → 对方确认后开始传输。中断任务可续传 |
| **文字** | 输入文字回车发送，所有人可见 |
| **电影院** | 房主选择电影播放 → 房客点「加入观影」→ 自动同步。支持全屏（Esc 退出） |

### 电影院详细流程
1. 房主将电影文件放入 `movies/` 文件夹
2. 点击「上传电影」或手动放入，列表自动刷新
3. 如需发给房客：选中电影 → 「发送给房客」（走文件传输，对方确认后接收）
4. 房主双击/点播放 → VLC 嵌入窗口开始播放
5. 房客切换至电影 Tab → 「加入观影」→ 自动加载本地文件并同步进度
6. 任何人点暂停 = 全员暂停；仅房主可拖动进度条

### 断点续传
1. 传输中断后重新连接房间
2. 文件 Tab 下方「中断的传输任务」列表中选中任务
3. 点击「继续传输」从断点处恢复

---

## 打包部署

运行 `build.bat` 生成单文件 `dist\Cuckoo.exe`：

```bash
build.bat
```

EXE 内已包含：
- Python 运行时
- VLC 运行时（libvlc + 395 个解码器插件）
- Radmin VPN 安装包（`Radmin_LAN_2.0.4899.9.exe`）
- 所有 Python 依赖

**发给朋友**：只需 `Cuckoo.exe` 一个文件。首次使用点「安装 Radmin」完成 VPN 安装，VLC 无需安装。

---

## 项目结构

```
Cuckoo/
├── main.py              # 程序入口
├── config.py            # 全局配置
├── build.bat            # 打包脚本
├── core/
│   ├── server.py        # 房主 TCP 服务器
│   ├── client.py        # 房客 TCP 连接
│   └── protocol.py      # 通信协议定义
├── common/
│   ├── logger.py        # 日志模块
│   └── network.py       # 网络工具
├── func/
│   ├── screen_share/    # 投屏（dxcam + turbojpeg）
│   ├── voice_chat/      # 语音（音频管道 + 混音器）
│   ├── file_transfer/   # 文件传输（chunk ACK + 断点续传）
│   └── cinema/          # 电影院（VLC 同步播放）
├── ui/
│   ├── main_window.py   # 主窗口
│   ├── start_dialog.py  # 启动对话框
│   ├── online_panel.py  # 在线列表
│   └── tabs/            # 各功能 Tab
│       ├── screen_tab.py
│       ├── voice_tab.py
│       ├── file_tab.py
│       ├── chat_tab.py
│       └── cinema_tab.py
└── runtime/             # 嵌入式 Python + VLC 运行时
```
