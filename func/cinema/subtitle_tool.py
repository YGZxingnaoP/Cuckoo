# -*- coding: utf-8 -*-
"""
字幕工具
使用 FFmpeg 提取 MKV 内嵌字幕（ASS/SRT），缩放 ASS 字号，合并 SRT 多行。
"""

import os
import re
import subprocess
import sys
from common import logger as log

TAG = "SubtitleTool"


def _get_ffmpeg_path() -> str:
    """查找 FFmpeg 可执行文件路径"""
    # 1) 开发模式：runtime/ffmpeg.exe
    dev = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "runtime", "ffmpeg.exe")
    if os.path.isfile(dev):
        return dev
    # 2) 打包模式：sys._MEIPASS/ffmpeg.exe
    if getattr(sys, 'frozen', False):
        pkg = os.path.join(sys._MEIPASS, "ffmpeg.exe")
        if os.path.isfile(pkg):
            return pkg
    # 3) PATH 中查找
    return "ffmpeg"


def extract_and_normalize(mkv_path: str, font_size: int) -> str | None:
    """
    提取 MKV 第一条字幕轨道，根据格式处理：
    - ASS：缩放 \\fs 标签 + Style 字号 → 写 .adjusted.ass
    - SRT：合并多行条目 → 写 .adjusted.srt
    返回外挂文件路径，失败返回 None。
    """
    raw = _ffmpeg_extract(mkv_path)
    if not raw:
        return None

    if "[Script Info]" in raw[:2000] or "[V4" in raw[:2000]:
        return _process_ass(raw, mkv_path, font_size)
    else:
        return _process_srt(raw, mkv_path)


def _ffmpeg_extract(mkv_path: str) -> str | None:
    """用 FFmpeg 提取第一个字幕流到内存"""
    try:
        ffmpeg = _get_ffmpeg_path()
        result = subprocess.run(
            [ffmpeg, "-y", "-i", mkv_path, "-map", "0:s:0",
             "-f", "srt", "pipe:1"],
            capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warn(TAG, f"FFmpeg unavailable: {e}")
        return None

    raw = result.stdout
    if len(raw) < 50:
        log.warn(TAG, f"FFmpeg returned too little data ({len(raw)}B)")
        return None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")

    log.log(TAG, f"FFmpeg extracted subtitle: {len(text)} bytes")
    return text


def _process_ass(raw: str, mkv_path: str, font_size: int) -> str | None:
    """处理 ASS 字幕：缩放所有字号引用"""
    base_fs = _get_ass_base_fs(raw)
    scale = font_size / 16.0
    target_fs = max(10, int(base_fs * scale))

    result = []
    for line in raw.splitlines(True):
        stripped = line.strip()
        if stripped.startswith("Style:"):
            parts = line.split(",", 3)
            if len(parts) >= 3:
                parts[2] = str(target_fs)
            result.append(",".join(parts))
        else:
            result.append(re.sub(r"\\fs\d+(\.\d+)?", f"\\fs{target_fs}", line))

    out_path = _adj_path(mkv_path, ".adjusted.ass")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(result))
    log.log(TAG, f"ASS saved: base_fs={base_fs}→{target_fs} → {out_path}")
    return out_path


def _process_srt(raw: str, mkv_path: str) -> str | None:
    """处理 SRT 字幕：把多行条目合并为单行"""
    blocks = re.split(r"\n\n+", raw.strip())
    out_blocks = []
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        # 保持序号行和时间行不变，后续行合并为一行
        header_lines = []
        body_lines = []
        for line in lines:
            if re.match(r"^\d+$", line) or "-->" in line:
                header_lines.append(line)
            else:
                body_lines.append(line)
        # 合并正文为一行（用空格连接）
        merged = " ".join(body_lines).strip()
        if header_lines and merged:
            out_blocks.append("\n".join(header_lines) + "\n" + merged)

    out_path = _adj_path(mkv_path, ".adjusted.srt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out_blocks) + "\n")
    log.log(TAG, f"SRT saved (lines joined) → {out_path}")
    return out_path


def _get_ass_base_fs(ass_text: str) -> int:
    for line in ass_text.splitlines():
        if line.strip().startswith("Style:"):
            parts = line.split(",")
            if len(parts) >= 3:
                try:
                    return int(parts[2].strip())
                except ValueError:
                    continue
    return 20


def _adj_path(mkv_path: str, ext: str) -> str:
    base = os.path.splitext(mkv_path)[0]
    return base + ext


def detect_type(mkv_path: str) -> str:
    """检测 MKV 字幕类型：'ass', 'srt', 或 None"""
    raw = _ffmpeg_extract(mkv_path)
    if not raw:
        return ""
    if "[Script Info]" in raw[:2000] or "[V4" in raw[:2000]:
        return "ass"
    return "srt"


# 保留旧接口兼容
def extract_ass(mkv_path: str) -> str | None:
    raw = _ffmpeg_extract(mkv_path)
    if raw and ("[Script Info]" in raw[:2000] or "[V4" in raw[:2000]):
        return raw
    return None

def get_base_font_size(ass_text: str) -> int:
    return _get_ass_base_fs(ass_text)

def scale_ass(ass_text: str, target_fs: int) -> str:
    result = []
    for line in ass_text.splitlines(True):
        s = line.strip()
        if s.startswith("Style:"):
            p = line.split(",", 3)
            if len(p) >= 3: p[2] = str(target_fs)
            result.append(",".join(p))
        else:
            result.append(re.sub(r"\\fs\d+(\.\d+)?", f"\\fs{target_fs}", line))
    return "".join(result)

def get_adj_ass_path(mkv_path: str) -> str:
    return _adj_path(mkv_path, ".adjusted.ass")
