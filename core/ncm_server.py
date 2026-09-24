"""内置 NeteaseCloudMusicApi 服务管理。

设计要点：
- 默认关闭：不启动任何进程。
- 二进制已随插件打包在 bin/ 目录（linux / windows / macOS x64），开箱即用；
  打包文件缺失时才回退到 GitHub Release 下载（支持镜像轮换与断点续传）。
- 仅在配置 ncm_api_embedded=true 且插件（重）启动时，以子进程方式启动服务
  （HOST=127.0.0.1，仅本机访问），健康检查通过后自动把插件音源切到内置服务。
- 关闭开关或插件卸载/重载时，终止子进程。

二进制来源（MIT 许可，允许分发）：
https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/releases
"""

import asyncio
import platform
import shutil
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

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=3600, connect=15, sock_read=60)

# 未配置 ncm_api_embedded_mirror 时的下载源候选：先直连，失败后换加速镜像
_MIRROR_CANDIDATES = [
    "",
    "https://ghfast.top/",
    "https://gh-proxy.com/",
]


# 插件包内打包的二进制目录（core/ 的上一级 /bin）
_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "bin"


class EmbeddedNcmServer:
    """内置 NeteaseCloudMusicApi 服务的二进制定位与进程管理"""

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
        """优先使用插件包内打包的二进制，其次用数据目录中已下载的副本"""
        bundled = _BUNDLED_DIR / self._asset_name()
        if bundled.exists() and bundled.stat().st_size > 1024 * 1024:
            return bundled
        return self.dir / self._asset_name()

    # ---------- 下载 ----------

    async def ensure_binary(self):
        """确保二进制可用：包内已打包则直接返回；否则从 Release 下载（兜底）。

        - 依次尝试：用户配置的镜像 → 直连 GitHub → 内置加速镜像；
        - 每个源失败自动换源，同一源最多试 2 次；
        - 下载到 .part 支持断点续传，完成后改名，避免半成品。
        """
        path = self.bin_path
        if path.exists() and path.stat().st_size > 1024 * 1024:
            return
        # 走到这里说明包内未打包且数据目录无副本，才需要联网下载
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        base = f"{RELEASE_BASE}/{self._asset_name()}"
        mirrors = [self.mirror] if self.mirror else list(_MIRROR_CANDIDATES)

        last_err: Exception | None = None
        for mirror in mirrors:
            url = f"{mirror}{base}" if mirror else base
            for attempt in range(2):
                try:
                    await self._download(url, tmp)
                    tmp.rename(path)
                    # 赋予执行权限（Windows 忽略）
                    try:
                        path.chmod(
                            path.stat().st_mode
                            | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                        )
                    except Exception:
                        pass
                    logger.info(f"[ncm_player] 内置服务下载完成: {path}")
                    return
                except Exception as e:
                    last_err = e
                    logger.warning(
                        f"[ncm_player] 内置服务下载失败"
                        f"（{'直连' if not mirror else mirror}，第 {attempt + 1} 次）: {e}"
                    )
                    await asyncio.sleep(2)
        raise RuntimeError(
            f"内置服务下载失败，所有下载源均不可用: {last_err}。"
            "可在插件配置 ncm_api_embedded_mirror 填写其他加速前缀，"
            "或配置 http_proxy 后重试"
        )

    async def _download(self, url: str, tmp: Path):
        """下载到 .part，已存在部分时带 Range 断点续传，并定期打印进度"""
        offset = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        logger.info(
            f"[ncm_player] 开始下载内置 NeteaseCloudMusicApi 服务: {url}"
            + (f"（从 {offset / 1024 / 1024:.1f}MB 续传）" if offset else "")
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, proxy=self.proxy, headers=headers, timeout=_DOWNLOAD_TIMEOUT
            ) as resp:
                if resp.status not in (200, 206):
                    raise RuntimeError(f"HTTP {resp.status}")
                # 服务器忽略 Range 返回 200 时从头重写
                mode = "ab" if (offset and resp.status == 206) else "wb"
                written = offset if mode == "ab" else 0
                total = (resp.content_length or 0) + (offset if mode == "ab" else 0)
                next_log = 0
                with open(tmp, mode) as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                        mb = written / 1024 / 1024
                        if mb >= next_log:
                            next_log = mb + 10
                            if total:
                                logger.info(
                                    f"[ncm_player] 内置服务下载进度: "
                                    f"{mb:.0f}/{total / 1024 / 1024:.0f}MB"
                                )
                if written < 1024 * 1024:
                    raise RuntimeError(f"下载内容异常（仅 {written} 字节）")

    # ---------- 进程管理 ----------

    def _ensure_executable(self) -> Path:
        """把二进制复制到可写的数据目录并赋予执行权限，返回可执行路径。

        插件目录（zip 安装 / 只读挂载）里的文件通常没有执行权限，
        直接运行会 Permission denied，因此统一复制到数据目录执行。
        """
        src = self.bin_path
        dst = self.dir / src.name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(str(src), str(dst))
        dst.chmod(0o755)
        return dst

    async def start(self) -> str:
        """确保二进制就绪并启动服务，返回服务地址。失败抛异常"""
        await self.ensure_binary()
        if self.process and self.process.returncode is None:
            return self.base_url  # 已在运行

        self.dir.mkdir(parents=True, exist_ok=True)  # 作为进程工作目录
        exe = self._ensure_executable()
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PORT": str(self.port),
            "HOST": "127.0.0.1",
        }
        self.process = await asyncio.create_subprocess_exec(
            str(exe),
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

    async def _wait_ready(self, timeout: int = 60):
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
