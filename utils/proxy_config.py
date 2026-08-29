"""
SOCKS5 代理统一配置入口

配置项 (config.json -> proxy)：
- enabled: bool      是否启用代理
- server: str        SOCKS5 代理地址，如 "127.0.0.1:1080"（可省略 socks5:// 前缀）
- remote_dns: bool   是否由代理服务端解析域名（remote DNS）
                     True  -> 使用 socks5h:// 前缀（域名交给代理端解析，本地不发 DNS 查询）
                     False -> 使用 socks5:// 前缀（本地解析域名后连接代理）
- check_interval: int  代理健康检查间隔（秒），0 表示不自动检查（默认 60）

代理健康监控：
- 后台守护线程按 check_interval 经代理探测业务域名；连续 2 次失败判定代理不可用，
  1 次成功即恢复。状态变化时自动 apply_proxy_env()（不可用清除 env / 恢复重设）。
- 不可用期间：requests 系 get_proxies() 返回空（直连）、websockets 跳过代理握手、
  Playwright 不传 proxy —— 均回退直连；恢复后自动切回代理。
- 注意：httpx/openai 长驻客户端在创建时读取 env，健康翻转对其不生效（需重启/重建）。

覆盖范围：
- requests（PDD API / 图片下载等）：各调用点显式传 get_proxies()（按 remote_dns 精确控制 socks5h/socks5）
- httpx / openai SDK / agno（AI 回复、embedder）：通过环境变量 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。
  注意：httpx 只接受 socks5:// scheme 且其 SOCKS5 实现固定由代理端解析域名（remote DNS），
  因此 env 一律设 socks5://，remote_dns 开关对 httpx 系不生效（始终远端解析）。
- websockets（拼多多消息通道）：websockets>=13 已移除内置 SOCKS 支持，由本模块的
  open_socks5_connection() 自行完成 SOCKS5 握手后把 socket 交给 websockets.connect(sock=...)，
  开关完全生效。
- Playwright（登录/店铺浏览器）：显式传 proxy 参数（不读环境变量）；
  Chromium 的 SOCKS5 始终由代理端解析 DNS，remote_dns 开关对其不生效
"""
import asyncio
import os
import socket
import threading
import time
from typing import Dict, Optional

# playwright 的 ProxySettings TypedDict（launch 的 proxy 参数类型）
from playwright._impl._api_structures import ProxySettings

# 标准环境变量名（小写变体也常见，但统一用大写）
_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

# 健康检查探活目标：业务核心域名，响应码 < 500 即视为代理通路可用
_PROBE_URL = "https://mms.pinduoduo.com/"
_PROBE_TIMEOUT = 5.0
# 连续失败达到该次数才判定代理不可用（避免偶发抖动误伤）
_FAIL_THRESHOLD = 2

# ---------------------------------------------------------------------------
# 代理健康状态（线程安全）
# ---------------------------------------------------------------------------
_health_lock = threading.RLock()
_proxy_health_ok = True            # 当前是否认为代理可用（未启用代理时恒为 True）
_consecutive_failures = 0          # 连续探活失败次数
_check_interval = 60.0             # 探活间隔（秒），0 表示不检查
_stop_event = threading.Event()
_monitor_thread: Optional[threading.Thread] = None


def is_proxy_healthy() -> bool:
    """代理健康状态：不可用（探活连续失败）返回 False；未启用代理恒为 True。"""
    with _health_lock:
        if not _get_enabled():
            return True
        return _proxy_health_ok


def _get_enabled() -> bool:
    from config import config
    return bool(config.get("proxy.enabled", False)) and bool(
        (config.get("proxy.server", "") or "").strip()
    )


def probe_proxy(timeout: float = _PROBE_TIMEOUT) -> bool:
    """同步探活：经代理访问业务域名，返回代理是否可用（不受健康状态影响，始终走代理）。"""
    url = get_proxy_url()
    if not url:
        return True  # 未启用代理，视为"可用"（无需回退）
    try:
        import requests
        resp = requests.get(
            _PROBE_URL,
            proxies={"http": url, "https": url},
            timeout=timeout,
        )
        return resp.status_code < 500
    except Exception:
        return False


def set_proxy_health(ok: bool, reason: str = "") -> None:
    """更新代理健康状态；状态翻转时应用/清除环境变量并记录日志。"""
    global _proxy_health_ok, _consecutive_failures
    with _health_lock:
        if ok:
            _consecutive_failures = 0
        changed = _proxy_health_ok != ok
        _proxy_health_ok = ok

    if changed:
        # 状态翻转：同步 env（httpx 系新 client 生效），并记录日志
        apply_proxy_env()
        try:
            from utils.logger_loguru import get_logger
            get_logger("ProxyHealth").warning(
                f"代理状态变化 -> {'可用' if ok else '不可用，已回退直连'}"
                + (f"（{reason}）" if reason else "")
            )
        except Exception:
            pass


def _health_loop() -> None:
    """后台探活循环：连续失败达阈值判不可用，1 次成功即恢复。"""
    global _consecutive_failures
    while not _stop_event.is_set():
        interval = _check_interval
        if interval <= 0:
            return
        if _stop_event.wait(interval):
            return
        ok = probe_proxy()
        with _health_lock:
            if ok:
                _consecutive_failures = 0
            else:
                _consecutive_failures += 1
        if not ok and _consecutive_failures >= _FAIL_THRESHOLD:
            set_proxy_health(False, reason=f"连续 {_consecutive_failures} 次探活失败")
        elif ok:
            set_proxy_health(True)


def start_proxy_health_monitor(interval: float) -> None:
    """启动代理健康检查后台线程（幂等）。

    Args:
        interval: 探活间隔（秒）；<=0 表示不自动检查
    """
    global _check_interval, _monitor_thread
    with _health_lock:
        _check_interval = float(interval)
        if interval <= 0:
            # 关闭检查：停掉已有线程
            _stop_event.set()
            _monitor_thread = None
            # 关闭检查时恢复初始健康状态，避免旧状态残留影响请求
            _proxy_health_ok = True
            _consecutive_failures = 0
            return
        if _monitor_thread is not None and _monitor_thread.is_alive():
            _stop_event.set()
            _monitor_thread.join(timeout=2)
        _stop_event.clear()
        _proxy_health_ok = True
        _consecutive_failures = 0
        _monitor_thread = threading.Thread(
            target=_health_loop, name="ProxyHealthMonitor", daemon=True
        )
        _monitor_thread.start()


def stop_proxy_health_monitor() -> None:
    """停止代理健康检查后台线程（幂等）。"""
    global _monitor_thread
    with _health_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            _stop_event.set()
            _monitor_thread.join(timeout=2)
        _monitor_thread = None


def get_proxy_url() -> Optional[str]:
    """返回带完整 scheme 的代理 URL（socks5h=远端解析 / socks5=本地解析）。

    未启用或地址为空时返回 None。
    """
    from config import config

    if not config.get("proxy.enabled", False):
        return None

    server = (config.get("proxy.server", "") or "").strip()
    if not server:
        return None

    # 兼容用户直接填 socks5:// 或 socks5h:// 前缀的情况
    if "://" in server:
        server = server.split("://", 1)[1]

    scheme = "socks5h" if config.get("proxy.remote_dns", True) else "socks5"
    return f"{scheme}://{server}"


def get_proxies() -> Dict[str, str]:
    """返回 requests 使用的 proxies 字典（未启用或代理不可用时为空 dict，即直连）。

    remote_dns=True -> socks5h://（代理端解析）；False -> socks5://（本地解析）。
    requests + PySocks 同时支持两种前缀。
    """
    if not is_proxy_healthy():
        return {}
    url = get_proxy_url()
    if not url:
        return {}
    return {"http": url, "https": url}


def apply_proxy_env() -> None:
    """将代理写入进程环境变量，使 httpx / openai(agno) 走 socks5。

    关闭或未启用时清除相关环境变量。
    注意：
    - 此处固定使用 socks5:// 前缀——httpx 只接受该 scheme（socks5h:// 会直接抛
      ValueError: Unknown scheme），且 httpx 的 SOCKS5 实现固定由代理端解析域名。
    - httpx/openai 客户端在创建时读取环境变量，已存在的长驻客户端（如 AI Agent）
      需重建/重启后才生效。
    - requests 系不走这里（各调用点显式传 get_proxies()，精确控制 remote_dns）。
    - 代理判定不可用时（健康监控回退直连）同样清除环境变量。
    """
    from config import config

    if not is_proxy_healthy():
        # 代理不可用：清除 env 回退直连（httpx 系新 client 直连）
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        return

    enabled = config.get("proxy.enabled", False)
    server = (config.get("proxy.server", "") or "").strip()
    if not enabled or not server:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        return

    if "://" in server:
        server = server.split("://", 1)[1]
    url = f"socks5://{server}"
    for key in _ENV_KEYS:
        os.environ[key] = url


async def open_socks5_connection(host: str, port: int, timeout: float = 30.0) -> Optional[socket.socket]:
    """通过 SOCKS5 代理建立到目标 (host, port) 的 TCP 连接，返回已握手的原生 socket。

    - 代理未启用时返回 None（调用方应走直连）
    - remote_dns=True（socks5h）：域名由代理端解析
    - remote_dns=False（socks5）：本地解析域名，向代理发 IP

    返回的原生 socket 可直接传给 websockets.connect(uri, sock=sock) 等
    接受预建连接的库（注意：不要用 asyncio transport 包装过的 socket，
    Windows Proactor 下 TransportSocket 无法被 create_connection 复用）。

    Args:
        host: 目标主机（域名或 IP）
        port: 目标端口
        timeout: 握手总超时（秒）

    Returns:
        已通过 SOCKS5 握手的原生 socket，或 None（未启用代理 / 代理判定不可用走直连）
    """
    if not is_proxy_healthy():
        return None
    url = get_proxy_url()
    if not url:
        return None

    from urllib.parse import urlparse

    parsed = urlparse(url)
    proxy_host = parsed.hostname
    proxy_port = parsed.port or 1080
    remote_dns = url.startswith("socks5h://")

    loop = asyncio.get_running_loop()

    async def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = await loop.sock_recv(sock, n - len(buf))
            if not chunk:
                raise ConnectionError("SOCKS5 代理连接提前关闭")
            buf += chunk
        return buf

    async def _handshake() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, (proxy_host, proxy_port))

            # 1. 协商认证方法（仅支持无认证）
            await loop.sock_sendall(sock, b"\x05\x01\x00")
            resp = await _recv_exact(sock, 2)
            if resp[0] != 0x05 or resp[1] != 0x00:
                raise ConnectionError(f"SOCKS5 认证协商失败: {resp!r}")

            # 2. 构造 CONNECT 目标地址
            if remote_dns:
                # 远端解析：ATYP=3 (DOMAINNAME)
                host_bytes = host.encode("idna")
                if len(host_bytes) > 255:
                    raise ValueError(f"目标域名过长: {host}")
                addr = b"\x03" + bytes([len(host_bytes)]) + host_bytes
            else:
                # 本地解析：先解析 DNS，向代理发 IP（ATYP=1/4）
                try:
                    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                except OSError as e:
                    raise ConnectionError(f"本地解析域名失败 {host}: {e}") from e
                ip = str(infos[0][4][0])
                try:
                    addr = b"\x01" + socket.inet_pton(socket.AF_INET, ip)
                except OSError:
                    try:
                        addr = b"\x04" + socket.inet_pton(socket.AF_INET6, ip)
                    except OSError as e:
                        raise ConnectionError(f"无法解析目标地址 {host}: {e}") from e

            # 3. 发送 CONNECT 请求
            await loop.sock_sendall(sock, b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))

            # 4. 读取响应，跳过 BND.ADDR/BND.PORT
            resp = await _recv_exact(sock, 4)
            if resp[0] != 0x05:
                raise ConnectionError(f"SOCKS5 响应版本错误: {resp!r}")
            if resp[1] != 0x00:
                raise ConnectionError(f"SOCKS5 连接被拒绝, code={resp[1]}")
            atyp = resp[3]
            if atyp == 0x01:      # IPv4
                await _recv_exact(sock, 4 + 2)
            elif atyp == 0x04:    # IPv6
                await _recv_exact(sock, 16 + 2)
            elif atyp == 0x03:    # 域名
                ln = (await _recv_exact(sock, 1))[0]
                await _recv_exact(sock, ln + 2)
            else:
                raise ConnectionError(f"SOCKS5 响应地址类型错误: {atyp}")

            return sock
        except Exception:
            sock.close()
            raise

    return await asyncio.wait_for(_handshake(), timeout=timeout)


def get_playwright_proxy() -> Optional[ProxySettings]:
    """Playwright launch/launch_persistent_context 的 proxy 参数。

    Playwright 仅接受 socks5:// scheme；Chromium 的 SOCKS5 代理始终由
    代理服务端解析域名，故 remote_dns 开关对浏览器不生效（总是远端解析）。
    代理判定不可用（回退直连）时返回 None。
    """
    if not is_proxy_healthy():
        return None
    url = get_proxy_url()
    if not url:
        return None
    server = url.replace("socks5h://", "socks5://")
    return {"server": server}
