"""内置 NeteaseCloudMusicApi 服务管理。

设计要点：
- 默认关闭：不下载任何文件、不启动任何进程。
- 仅在配置 ncm_api_embedded=true 且插件（重）启动时：
  1. 检测数据目录中是否已有对应平台的预编译二进制，没有则从 GitHub Release 下载；
  2. 以子进程方式启动服务（HOST=127.0.0.1，仅本机访问）；
  3. 健康检查通过后，自动把插件音源切到内置服务。
- 关闭开关或插件卸载/重载时，终止子进程；已下载的二进制保留，下次开启直接使用。

二进制来源（MIT 许可，允许分发）：
https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/releases
"""

import asyncio
import platform
import stat
from pathlib import Path

import aiohttp

from astrbot.api import logger

# 固定版本，避免上游发版导致行为漂移；需要升级时改这里即可
RELEASE_TAG = "v4.40.1"
RELEASE_BASE = (
    "https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced"
    f"/releases/download/{RELEASE_TAG}"
)

# (系统, 机器架构) -> Release 资产名
_ASSETS = {
    ("linux", "x86_64"): "ncm-api-linux-x64",
    ("linux", "amd64"): "ncm-api-linux-x64",
    ("darwin", "x86_64"): "ncm-api-macos-x64",
    ("darwin", "arm64"): "ncm-api-macos-x64",  # Apple Silicon 走 Rosetta
    ("windows", "amd64"): "ncm-api-win-x64.exe",
    ("windows", "x86_64"): "ncm-api-win-x64.exe",
}

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600, connect=15, sock_read=60)


class EmbeddedNcmServer:
    """内置 NeteaseCloudMusicApi 服务的下载与进程管理"""

    def __init__(
        self,
        data_dir: Path,
        port: int = 13000,
        proxy: str = "",
        mirror: str = "",
    ):
        self.dir = Path(data_dir) / "ncm_api_server"
        self.port = int(port)
        self.proxy = proxy or None
        # 下载镜像前缀，如 https://ghfast.top/ ，留空则直连 GitHub
        self.mirror = (mirror or "").strip()
        self.process: asyncio.subprocess.Process | None = None

    # ---------- 路径 / 平台 ----------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _asset_name(self) -> str:
        sys_name = platform.system().lower()
        machine = platform.machine().lower()
        asset = _ASSETS.get((sys_name, machine))
        if not asset:
            raise RuntimeError(
                f"内置服务暂不支持当前平台 {sys_name}/{machine}，"
                "请改用 ncm_api_base 配置外部 NeteaseCloudMusicApi 服务"
            )
        return asset

    @property
    def bin_path(self) -> Path:
        return self.dir / self._asset_name()

    def _download_url(self) -> str:
        url = f"{RELEASE_BASE}/{self._asset_name()}"
        return f"{self.mirror}{url}" if self.mirror else url

    # ---------- 下载 ----------

    async def ensure_binary(self):
        """二进制不存在则下载（约 70MB）。下载到 .part 完成后改名，避免半成品"""
        path = self.bin_path
        if path.exists() and path.stat().st_size > 1024 * 1024:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        url = self._download_url()
        logger.info(f"[ncm_player] 开始下载内置 NeteaseCloudMusicApi 服务: {url}")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, proxy=self.proxy, timeout=_DOWNLOAD_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
        tmp.rename(path)
        # 赋予执行权限（Windows 忽略）
        try:
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            pass
        logger.info(f"[ncm_player] 内置服务下载完成: {path}")

    # ---------- 进程管理 ----------

    async def start(self) -> str:
        """确保二进制就绪并启动服务，返回服务地址。失败抛异常"""
        await self.ensure_binary()
        if self.process and self.process.returncode is None:
            return self.base_url  # 已在运行

        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PORT": str(self.port),
            "HOST": "127.0.0.1",
        }
        self.process = await asyncio.create_subprocess_exec(
            str(self.bin_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=str(self.dir),
        )
        logger.info(
            f"[ncm_player] 内置 NeteaseCloudMusicApi 服务已启动 "
            f"(pid={self.process.pid}, {self.base_url})"
        )
        await self._wait_ready()
        return self.base_url

    async def _wait_ready(self, timeout: int = 45):
        """健康检查：等待服务可连接"""
        async with aiohttp.ClientSession() as session:
            for _ in range(timeout):
                if self.process and self.process.returncode is not None:
                    raise RuntimeError(
                        f"内置服务进程异常退出(code={self.process.returncode})"
                    )
                try:
                    async with session.get(
                        f"{self.base_url}/",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as resp:
                        if resp.status < 500:
                            return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError(f"内置服务启动超时（{timeout} 秒内未就绪）")

    async def stop(self):
        """终止服务进程（不删除已下载的二进制）"""
        if not self.process:
            return
        proc, self.process = self.process, None
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        logger.info("[ncm_player] 内置 NeteaseCloudMusicApi 服务已停止")
