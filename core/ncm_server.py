"""管理插件自有 API：Release 下载、校验、隔离运行、删除和重装。

所有可写文件均位于 StarTools.get_data_dir() 下。仅操作自己创建的子进程，
不会删除系统临时目录或停止外部 API。上游项目及 MIT 许可见 README。
"""

import asyncio
import hashlib
import os
import platform
import re
import shutil
import socket
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import logger

RELEASE_TAG = "v4.40.1"
RELEASE_BASE = (
    "https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced"
    f"/releases/download/{RELEASE_TAG}"
)
# 固定版本的 GitHub Release 资产摘要，防止加速站错误页面/半成品被执行。
ASSET_DIGESTS = {
    "ncm-api-linux-x64": (
        74628988,
        "295c2cc170332b29fe7d895724131007385543d5ad982c6375a3eab68f69056c",
    ),
    "ncm-api-macos-x64": (
        80257200,
        "7b783ef1be51d8b8a330ebcd8d6262b9994e2c89f5e835d8a55eb73c65ae576c",
    ),
    "ncm-api-win-x64.exe": (
        66018387,
        "756ccbedda4593d65b6e8aa8139af8a93224fe8339f3a70308bd04be0c2e8424",
    ),
}
_ASSETS = {
    ("linux", "x86_64"): "ncm-api-linux-x64",
    ("linux", "amd64"): "ncm-api-linux-x64",
    ("darwin", "x86_64"): "ncm-api-macos-x64",
    ("darwin", "arm64"): "ncm-api-macos-x64",  # 需要系统安装 Rosetta。
    ("windows", "amd64"): "ncm-api-win-x64.exe",
    ("windows", "x86_64"): "ncm-api-win-x64.exe",
}
DEFAULT_ACCELERATORS = ["https://ghfast.top/", "https://gh-proxy.com/"]
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=3600, connect=15, sock_read=60)
_SENSITIVE_LOG = re.compile(
    r"cookie|authorization|MUSIC_[UA]|csrf|token|password|captcha|phone|二维码|验证码",
    re.I,
)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def sanitize_api_log(text: str) -> str:
    """API 可能打印请求体，敏感行整体省略，不把凭据带进诊断和 AstrBot 日志。"""
    text = _ANSI.sub("", text).strip()
    if _SENSITIVE_LOG.search(text):
        return "[敏感 API 日志已省略]"
    return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已省略]", text)[:1000]


class EmbeddedNcmServer:
    def __init__(
        self,
        data_dir: Path,
        port: int = 13000,
        proxy: str = "",
        mirror: str = "",
        acceleration_enabled: bool = True,
        accelerators: list[str] | None = None,
        log_output: bool = False,
    ):
        self.dir = Path(data_dir).resolve() / "ncm_api_server"
        self.runtime_dir = self.dir / "runtime"
        self.port = int(port)
        self.proxy = proxy or None
        self.acceleration_enabled = acceleration_enabled
        configured = DEFAULT_ACCELERATORS if accelerators is None else accelerators
        if not isinstance(configured, list):
            raise ValueError("GitHub 加速地址必须是列表")
        self.accelerators = ([mirror] if mirror else []) + configured
        self.log_output = log_output
        self.process: asyncio.subprocess.Process | None = None
        self._log_tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self.recent_logs: deque[str] = deque(maxlen=20)
        self.last_error = ""
        self.version = ""
        self.state = "未启动"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _asset_name(self) -> str:
        key = (platform.system().lower(), platform.machine().lower())
        if key not in _ASSETS:
            raise RuntimeError(f"内置服务不支持 {key[0]}/{key[1]}，请使用外部 API")
        return _ASSETS[key]

    @property
    def bin_path(self) -> Path:
        return self.dir / self._asset_name()

    def download_urls(self) -> list[str]:
        official = f"{RELEASE_BASE}/{self._asset_name()}"
        urls = []
        if self.acceleration_enabled:
            for prefix in self.accelerators:
                prefix = str(prefix).strip()
                parts = urlsplit(prefix)
                if (
                    parts.scheme in ("http", "https")
                    and parts.netloc
                    and not parts.query
                    and not parts.fragment
                ):
                    urls.append(f"{prefix.rstrip('/')}/{official}")
        return list(dict.fromkeys([*urls, official]))

    def _verify_binary(self, path: Path) -> bool:
        size, expected = ASSET_DIGESTS[self._asset_name()]
        if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected

    async def ensure_binary(self):
        self._check_owned_dir()
        path = self.bin_path
        if await asyncio.to_thread(self._verify_binary, path):
            path.chmod(0o755)
            return
        self.state = "下载中"
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        size, _ = ASSET_DIGESTS[self._asset_name()]
        for url in self.download_urls():
            # 只在同一次、同一个源的重试中续传，不能拼接不同镜像的响应。
            tmp.unlink(missing_ok=True)
            for attempt in range(2):
                try:
                    await self._download(url, tmp, size)
                    if not await asyncio.to_thread(self._verify_binary, tmp):
                        tmp.unlink(missing_ok=True)
                        raise ValueError("Release 文件大小或 SHA-256 不匹配")
                    os.replace(tmp, path)
                    path.chmod(0o755)
                    logger.info(f"[ncm_player] API {RELEASE_TAG} 下载校验完成")
                    return
                except (
                    ValueError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    OSError,
                ) as exc:
                    logger.warning(
                        f"[ncm_player] API 下载源 {urlsplit(url).hostname} "
                        f"第 {attempt + 1} 次失败: {type(exc).__name__}"
                    )
                    if isinstance(exc, ValueError):
                        tmp.unlink(missing_ok=True)
        raise RuntimeError("API 所有下载源失败，请检查 GitHub 加速地址、网络或代理")

    async def _download(self, url: str, tmp: Path, expected_size: int):
        offset = tmp.stat().st_size if tmp.exists() else 0
        if offset >= expected_size:
            tmp.unlink()
            offset = 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT) as session:
            async with session.get(url, proxy=self.proxy, headers=headers) as resp:
                resp.raise_for_status()
                if resp.status == 206:
                    match = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)",
                        resp.headers.get("Content-Range", ""),
                    )
                    if (
                        not match
                        or int(match[1]) != offset
                        or int(match[3]) != expected_size
                    ):
                        raise ValueError("无效的续传 Content-Range")
                elif resp.status != 200:
                    raise ValueError(f"意外下载状态 {resp.status}")
                append = offset > 0 and resp.status == 206
                written = offset if append else 0
                with tmp.open("ab" if append else "wb") as file:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        written += len(chunk)
                        if written > expected_size:
                            raise ValueError("下载超过已发布资产大小")
                        file.write(chunk)
                if written != expected_size:
                    raise aiohttp.ClientPayloadError("Release 下载不完整")

    def _check_owned_dir(self):
        if self.dir.is_symlink() or self.dir.resolve() != self.dir:
            raise RuntimeError("API 托管目录不是独立普通目录，拒绝操作")

    def _check_port(self):
        if not 1 <= self.port <= 65535:
            raise ValueError("API 端口必须在 1-65535 之间")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", self.port))
            except OSError as exc:
                raise RuntimeError(
                    f"端口 {self.port} 已被占用或无法绑定，请修改内置服务端口"
                ) from exc

    async def _read_logs(self, stream: asyncio.StreamReader):
        # 使用分块读取，防止上游单行过长导致 readline 抛错后管道堵塞。
        pending = b""
        while chunk := await stream.read(4096):
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                self._log_line(line)
            if len(pending) > 8192:
                self._log_line(pending)
                pending = b""
        if pending:
            self._log_line(pending)

    def _log_line(self, raw: bytes):
        line = sanitize_api_log(raw.decode("utf-8", errors="replace"))
        if line:
            self.recent_logs.append(line)
            if self.log_output:
                logger.info(f"[ncm_api] {line}")

    async def _start(self) -> str:
        if self.process and self.process.returncode is None and self.state == "运行中":
            return self.base_url
        self.last_error = ""
        self.recent_logs.clear()
        try:
            self._check_port()
            await self.ensure_binary()
            if (
                self.runtime_dir.is_symlink()
                or self.runtime_dir.resolve() != self.runtime_dir
            ):
                raise RuntimeError("API 临时目录必须位于插件托管目录内")
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            # 上游 os.tmpdir() 中的 anonymous_token / xeapi_public_key 必须隔离。
            env.update(
                {
                    "PORT": str(self.port),
                    "HOST": "127.0.0.1",
                    "TMP": str(self.runtime_dir),
                    "TEMP": str(self.runtime_dir),
                    "TMPDIR": str(self.runtime_dir),
                }
            )
            self.state = "启动中"
            self.process = await asyncio.create_subprocess_exec(
                str(self.bin_path),
                cwd=str(self.dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._log_tasks = [
                asyncio.create_task(self._read_logs(stream))
                for stream in (self.process.stdout, self.process.stderr)
            ]
            await self._wait_ready()
            self.state = "运行中"
            logger.info(
                f"[ncm_player] 内置 API {self.version} 已就绪 ({self.base_url})"
            )
            return self.base_url
        except asyncio.CancelledError:
            await self._stop()
            raise
        except Exception as exc:
            await self._stop()
            self.last_error = sanitize_api_log(str(exc))
            self.state = "启动失败"
            raise

    async def _wait_ready(self, timeout: float = 60):
        deadline = asyncio.get_running_loop().time() + timeout
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=2)
        ) as session:
            while asyncio.get_running_loop().time() < deadline:
                if not self.process or self.process.returncode is not None:
                    code = self.process.returncode if self.process else "无进程"
                    raise RuntimeError(f"API 子进程退出(code={code})")
                try:
                    async with session.get(f"{self.base_url}/inner/version") as resp:
                        data = await resp.json(content_type=None)
                        version = str((data.get("data") or {}).get("version") or "")
                        if (
                            resp.status == 200
                            and data.get("code") == 200
                            and version.lstrip("v") == RELEASE_TAG.lstrip("v")
                        ):
                            self.version = version
                            return
                except (
                    aiohttp.ClientError,
                    ValueError,
                    asyncio.TimeoutError,
                    AttributeError,
                ):
                    pass
                await asyncio.sleep(0.25)
        raise RuntimeError("API 启动超时或版本健康检查失败")

    async def _stop(self):
        proc = self.process
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()  # 回收后才能删除 Windows 下的可执行文件。
        self.process = None
        for task in self._log_tasks:
            task.cancel()
        await asyncio.gather(*self._log_tasks, return_exceptions=True)
        self._log_tasks.clear()
        self.state = "已停止"
        self.version = ""

    async def start(self) -> str:
        async with self._lock:
            return await self._start()

    async def stop(self):
        async with self._lock:
            await self._stop()

    async def _remove_files(self):
        self._check_owned_dir()
        if self.dir.exists():
            await asyncio.to_thread(shutil.rmtree, self.dir)

    async def remove(self):
        async with self._lock:
            await self._stop()
            await self._remove_files()
            self.state = "已删除"

    async def reinstall(self) -> str:
        async with self._lock:
            await self._stop()
            await self._remove_files()
            return await self._start()
