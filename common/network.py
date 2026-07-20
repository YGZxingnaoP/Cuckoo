# -*- coding: utf-8 -*-
"""
公共网络模块
封装 TCP / UDP 基础操作，供各功能模块复用。
"""

import socket
import struct
from typing import Optional

from common import logger as log

TAG = "Network"


# ─────────────────────────────────────────────
# TCP 工具
# ─────────────────────────────────────────────

def tcp_connect(host: str, port: int, timeout: float = 5.0) -> socket.socket:
    """创建 TCP 连接并返回 socket；失败时抛出 OSError。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((host, port))
    sock.settimeout(None)
    log.log(TAG, f"TCP connected to {host}:{port}")
    return sock


def tcp_send_all(sock: socket.socket, data: bytes) -> None:
    """确保全部字节发送完毕（阻塞）。"""
    sock.sendall(data)


def tcp_recv_exact(sock: socket.socket, n: int) -> bytes:
    """精确接收 n 字节。若连接断开则抛出 ConnectionError。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("TCP connection closed by peer")
        buf.extend(chunk)
    return bytes(buf)


# ─────────────────────────────────────────────
# UDP 工具
# ─────────────────────────────────────────────

def udp_socket(bind_addr: tuple[str, int], timeout: Optional[float] = None) -> socket.socket:
    """创建并绑定 UDP socket。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(bind_addr)
    if timeout is not None:
        sock.settimeout(timeout)
    log.log(TAG, f"UDP bound to {bind_addr}")
    return sock


def udp_send(sock: socket.socket, data: bytes, addr: tuple[str, int]) -> None:
    """发送 UDP 数据报。"""
    sock.sendto(data, addr)


def udp_recv(sock: socket.socket, bufsize: int = 65535) -> tuple[bytes, tuple[str, int]]:
    """接收 UDP 数据报，返回 (data, address)。超时时抛出 socket.timeout。"""
    return sock.recvfrom(bufsize)


# ─────────────────────────────────────────────
# 端口探测
# ─────────────────────────────────────────────

def probe_tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    """短连接探测 TCP 端口是否可达。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        return True
    except (socket.timeout, OSError):
        return False
