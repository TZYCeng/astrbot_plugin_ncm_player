"""显式启用的 Release 冒烟：真实二进制下载/启动/重装/删除，无真实账号。"""

import asyncio
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from .support import serve, server_module


@unittest.skipUnless(
    os.environ.get("NCM_RELEASE_SMOKE") == "1", "需显式启用真实 Release 下载"
)
class ReleaseSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_release_lifecycle(self):
        with tempfile.TemporaryDirectory(
            prefix="ncm-release-", dir=os.environ.get("NCM_SMOKE_DIR")
        ) as tmp:
            data = Path(tmp)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            server = server_module.EmbeddedNcmServer(data, port=port)
            self.addAsyncCleanup(server.stop)
            try:
                base = await asyncio.wait_for(server.start(), timeout=300)
                self.assertEqual(
                    server.version.lstrip("v"), server_module.RELEASE_TAG.lstrip("v")
                )
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{base}/inner/version", json={"cookie": {}}
                    ) as resp:
                        self.assertEqual((await resp.json())["code"], 200)
                print(
                    f"REAL RELEASE ready: {server.version}, isolated tmp={server.runtime_dir}"
                )
                self.assertTrue(server.runtime_dir.is_dir())
                # 验证真实上游确实把匿名凭据写在私有 tmp，而非仅创建了空目录。
                anonymous = server.runtime_dir / "anonymous_token"
                self.assertTrue(anonymous.is_file())
                # 一次公网下载已足够。重装从本地 HTTP 服务重新传输同一份真实资产，
                # 仍经过全部下载、SHA-256、进程启动及健康检查逻辑。
                copy = data / "release-fixture.bin"
                await asyncio.to_thread(shutil.copy2, server.bin_path, copy)
                marker = server.runtime_dir / "old-cache-marker"
                marker.write_text("must be removed")
                anonymous.write_text("stale-smoke-cache")
                cookie = data / "ncm_cookie.txt"
                cookie.write_text("fixture-not-a-real-cookie")

                async def asset(request):
                    return web.FileResponse(copy)

                async with serve(asset) as fixture_url:
                    with patch.object(
                        server, "download_urls", return_value=[fixture_url]
                    ):
                        await asyncio.wait_for(server.reinstall(), timeout=120)
                self.assertFalse(marker.exists())
                self.assertTrue(anonymous.is_file())
                self.assertNotEqual(anonymous.read_text(), "stale-smoke-cache")
                self.assertTrue(cookie.exists())
                await server.remove()
                self.assertFalse(server.dir.exists())
                self.assertIsNone(server.process)
                print(
                    "REAL RELEASE reinstall + cache isolation + process reap + delete: OK"
                )
            except BaseException:
                print(
                    f"REAL RELEASE diagnostic: {server.state}, {server.last_error}, {list(server.recent_logs)}"
                )
                raise
            finally:
                await server.stop()
