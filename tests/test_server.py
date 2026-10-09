import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from aiohttp import web

from .support import serve, server_module

EmbeddedNcmServer = server_module.EmbeddedNcmServer


class ServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.server = EmbeddedNcmServer(self.data)

    async def asyncTearDown(self):
        await self.server.stop()

    async def test_bad_mirror_rotates_and_digest_verified(self):
        payload = b"verified release fixture"
        calls = []

        async def handler(request):
            calls.append(request.path)
            return web.Response(
                body=b"x" * len(payload) if request.path == "/bad" else payload
            )

        async with serve(handler) as base:
            asset = self.server._asset_name()
            digest = hashlib.sha256(payload).hexdigest()
            with (
                patch.dict(
                    server_module.ASSET_DIGESTS, {asset: (len(payload), digest)}
                ),
                patch.object(
                    self.server,
                    "download_urls",
                    return_value=[base + "/bad", base + "/good"],
                ),
            ):
                self.server.dir.mkdir()
                self.server.bin_path.write_bytes(b"x" * len(payload))
                await self.server.ensure_binary()
                self.assertEqual(self.server.bin_path.read_bytes(), payload)
                self.assertEqual(calls, ["/bad", "/bad", "/good"])
                await self.server.ensure_binary()
                self.assertEqual(len(calls), 3, "校验通过的缓存无需下载")

    async def test_range_resume_rejects_wrong_offset_and_truncates_200(self):
        payload = b"complete-file"
        mode = "bad"

        async def handler(request):
            self.assertEqual(request.headers.get("Range"), "bytes=4-")
            if mode == "bad":
                return web.Response(
                    status=206,
                    body=payload[4:],
                    headers={"Content-Range": f"bytes 0-12/{len(payload)}"},
                )
            if mode == "resume":
                return web.Response(
                    status=206,
                    body=payload[4:],
                    headers={
                        "Content-Range": f"bytes 4-{len(payload) - 1}/{len(payload)}"
                    },
                )
            return web.Response(body=payload)

        path = self.data / "download.part"
        async with serve(handler) as base:
            path.write_bytes(payload[:4])
            with self.assertRaises(ValueError):
                await self.server._download(base, path, len(payload))
            mode = "resume"
            await self.server._download(base, path, len(payload))
            self.assertEqual(path.read_bytes(), payload)
            mode = "ignore-range"
            path.write_bytes(b"junk")
            await self.server._download(base, path, len(payload))
            self.assertEqual(path.read_bytes(), payload)

    async def test_accelerator_switch_controls_every_prefix(self):
        server = EmbeddedNcmServer(
            self.data,
            mirror="https://custom.test/",
            accelerators=["https://a.test/", "https://b.test/"],
        )
        self.assertEqual(len(server.download_urls()), 4)
        self.assertTrue(
            server.download_urls()[0].startswith(
                "https://custom.test/https://github.com/"
            )
        )
        server.acceleration_enabled = False
        self.assertEqual(
            server.download_urls(),
            [f"{server_module.RELEASE_BASE}/{server._asset_name()}"],
        )

    async def test_start_env_logs_and_remove_only_owned_files(self):
        saved = self.data / "ncm_cookie.txt"
        saved.write_text("MUSIC_U=preserve")
        self.server.dir.mkdir()
        self.server.bin_path.write_bytes(b"test")
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data(b"startup message\nMUSIC_U=secret\n")
        stdout.feed_eof()
        stderr.feed_eof()
        process = SimpleNamespace(returncode=None, stdout=stdout, stderr=stderr)
        process.terminate = lambda: None

        async def wait():
            process.returncode = 0
            return 0

        process.wait = wait
        with (
            patch.object(self.server, "_check_port"),
            patch.object(self.server, "ensure_binary", new_callable=AsyncMock),
            patch.object(
                self.server,
                "_wait_ready",
                new_callable=AsyncMock,
            ),
            patch.object(
                server_module.asyncio,
                "create_subprocess_exec",
                new_callable=AsyncMock,
                return_value=process,
            ) as spawn,
        ):
            await self.server.start()
            await asyncio.gather(*self.server._log_tasks)
            env = spawn.call_args.kwargs["env"]
            self.assertEqual(env["TMP"], str(self.server.runtime_dir))
            self.assertEqual(env["TMP"], env["TEMP"])
            self.assertEqual(env["TMP"], env["TMPDIR"])
            self.assertEqual(spawn.call_args.kwargs["cwd"], str(self.server.dir))
            self.assertTrue(self.server.runtime_dir.is_dir())
            self.assertNotIn("secret", "\n".join(self.server.recent_logs))
            await self.server.remove()
            self.assertEqual(process.returncode, 0)
            self.assertFalse(self.server.dir.exists())
            self.assertEqual(saved.read_text(), "MUSIC_U=preserve")

    async def test_port_conflict_does_not_download_or_stop_other_server(self):
        async def handler(request):
            return web.Response(text="other process")

        async with serve(handler) as base:
            self.server.port = urlsplit(base).port
            with patch.object(
                self.server, "ensure_binary", new_callable=AsyncMock
            ) as download:
                with self.assertRaisesRegex(RuntimeError, "端口"):
                    await self.server.start()
                download.assert_not_awaited()
            self.assertEqual(self.server.state, "启动失败")

    async def test_health_requires_api_version_not_any_http_200(self):
        correct = False

        async def handler(request):
            if correct and request.path == "/inner/version":
                return web.json_response(
                    {"code": 200, "data": {"version": server_module.RELEASE_TAG[1:]}}
                )
            return web.Response(text="Welcome")

        async with serve(handler) as base:
            self.server.port = urlsplit(base).port
            self.server.process = SimpleNamespace(returncode=None)
            try:
                with self.assertRaises(RuntimeError):
                    await self.server._wait_ready(timeout=0.05)
                correct = True
                await self.server._wait_ready(timeout=1)
                self.assertEqual(self.server.version, server_module.RELEASE_TAG[1:])
            finally:
                self.server.process = None

    async def test_log_forwarding_default_off_and_opt_in(self):
        with patch.object(server_module.logger, "info") as log:
            self.server._log_line(b"service started")
            log.assert_not_called()
            self.server.log_output = True
            self.server._log_line(b"Cookie: MUSIC_U=secret")
            self.assertEqual(log.call_count, 1)
            self.assertNotIn("secret", str(log.call_args))
