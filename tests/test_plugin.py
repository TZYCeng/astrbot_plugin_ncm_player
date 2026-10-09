import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from .support import (
    Config,
    Event,
    StarTools,
    account_payload,
    api_module,
    auth,
    main_module,
    serve,
)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.cfg = Config(
            ncm_api_external_enabled=True,
            ncm_api_base="http://external.invalid",
            ncm_web_enabled=False,
            send_play_card=False,
            send_comment_card=False,
            send_lyrics_forward=False,
            load_mode="file",
        )
        with patch.object(StarTools, "get_data_dir", return_value=self.data):
            self.plugin = main_module.NcmPlayerPlugin(None, self.cfg)
        self.addAsyncCleanup(self.plugin.terminate)

    async def test_source_switches_allow_all_four_combinations(self):
        for inner in (False, True):
            for outer in (False, True):
                self.cfg["ncm_api_embedded"] = inner
                self.cfg["ncm_api_external_enabled"] = outer
                self.plugin._update_sources("http://inner.invalid" if inner else "")
                self.assertEqual(
                    [url for _, url in self.plugin.api.api_sources],
                    (["http://inner.invalid"] if inner else [])
                    + (["http://external.invalid"] if outer else []),
                )

    async def test_delete_persists_off_and_reinstall_enables(self):
        server = self.plugin.embedded
        server.runtime_dir.mkdir(parents=True)
        (server.runtime_dir / "anonymous_token").write_text("owned cache")
        (server.dir / "executable").write_text("owned binary")
        self.plugin.cookie_file.write_text("MUSIC_U=preserved")
        self.cfg["ncm_api_embedded"] = True
        with patch.object(self.plugin, "_probe_accounts", new_callable=AsyncMock):
            message = await self.plugin._manage_api(False)
            self.assertIn("已删除", message)
            self.assertFalse(self.cfg.saved["ncm_api_embedded"])
            self.assertFalse(server.dir.exists())
            self.assertTrue(self.plugin.cookie_file.exists())
            self.assertEqual(
                self.plugin.api.api_sources, [("外部 API", "http://external.invalid")]
            )
            with patch.object(
                server,
                "reinstall",
                new_callable=AsyncMock,
                return_value=server.base_url,
            ) as reinstall:
                await self.plugin._manage_api(True)
                reinstall.assert_awaited_once()
            self.assertTrue(self.cfg.saved["ncm_api_embedded"])
            self.assertEqual(
                self.plugin.api.api_sources[0], ("内置 API", server.base_url)
            )

    async def test_config_save_failure_does_not_delete(self):
        self.cfg["ncm_api_embedded"] = True
        with (
            patch.object(self.cfg, "save_config", side_effect=OSError("denied")),
            patch.object(
                self.plugin.embedded,
                "remove",
                new_callable=AsyncMock,
            ) as remove,
        ):
            result = await self.plugin._manage_api(False)
            self.assertIn("操作已取消", result)
            self.assertTrue(self.cfg["ncm_api_embedded"])
            remove.assert_not_awaited()

    async def test_failed_audio_download_reaches_mirror_and_outer(self):
        requests = []

        async def handler(request):
            requests.append(request.path)
            if request.path == "/song/url/v1":
                return web.json_response(
                    {"data": [{"url": base + "/broken", "level": "standard"}]}
                )
            if request.path in ("/broken", "/mirror"):
                return web.Response(text="not audio", content_type="text/html")
            if request.path == "/outer":
                return web.Response(body=b"audio fixture", content_type="audio/mpeg")
            return web.json_response({"code": 200})

        async with serve(handler) as base:
            api = self.plugin.api
            api.set_api_sources([("本地测试", base)])
            api.meting_api = base + "/mirror"
            api.OUTER_URL = base + "/outer"
            self.cfg["quality"] = "standard"
            self.plugin.sender.send = AsyncMock(return_value="语音")
            event = Event()
            # 真实下载失败仍测试两次请求，仅省略生产环境的重试退避时间。
            with patch.object(api_module.asyncio, "sleep", new_callable=AsyncMock):
                result = await self.plugin._play(
                    event, api_module.Song(1, "测试", "歌手")
                )
            self.assertIn("官方外链", result)
            self.assertEqual(
                requests,
                ["/song/url/v1", "/broken", "/broken", "/mirror", "/mirror", "/outer"],
            )
            play = self.plugin.sender.send.call_args.args[2]
            path = self.plugin.sender.send.call_args.args[3]
            self.assertEqual(play.source, "outer")
            self.assertEqual(Path(path).read_bytes(), b"audio fixture")
            # 协议端可能仍在读取成功下载的文件，手动清缓存不能提前删掉它。
            _ = [text async for text in self.plugin.cmd_clear_cache(event)]
            self.assertTrue(Path(path).exists())
            self.plugin._expire_audio(Path(path))
            self.assertFalse(Path(path).exists())

    async def test_cancelled_download_removes_partial_and_releases_cache_guard(self):
        started, hold = asyncio.Event(), asyncio.Event()

        async def download(url, dest, **kwargs):
            Path(dest).write_bytes(b"partial")
            started.set()
            await hold.wait()

        with patch.object(self.plugin.api, "download", side_effect=download):
            task = asyncio.create_task(
                self.plugin._download(
                    api_module.Song(1, "测试", "歌手"),
                    api_module.PlayInfo(url="https://audio.invalid"),
                )
            )
            await asyncio.wait_for(started.wait(), 2)
            paths = list(self.plugin._active_downloads)
            self.assertEqual(len(paths), 1)
            _ = [text async for text in self.plugin.cmd_clear_cache(Event())]
            self.assertTrue(paths[0].exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(paths[0].exists())
            self.assertEqual(self.plugin._active_downloads, set())

    async def test_login_persistence_normalizes_and_rejects_stale_generation(self):
        async def handler(request):
            if request.path == "/login/status":
                return web.json_response(account_payload("correct"))
            return web.json_response({"code": 200, "data": {}})

        async with serve(handler) as base:
            raw = "__csrf=a; Path=/;MUSIC_U=user==; Max-Age=42"
            candidate = api_module.NetEaseAPI(ncm_api_base=base, cookie=raw)
            self.addAsyncCleanup(candidate.close)
            result = await self.plugin._save_login(raw, candidate, 0, "扫码")
            self.assertIn("UID correct", result)
            self.assertEqual(
                self.plugin.cookie_file.read_text(), "__csrf=a; MUSIC_U=user=="
            )
            self.assertEqual(self.plugin.api.account.uid, "correct")
            self.plugin._login_generation += 1
            candidate.set_cookie("MUSIC_U=other")
            result = await self.plugin._save_login(
                "MUSIC_U=other", candidate, 0, "扫码"
            )
            self.assertIn("取消", result)
            self.assertEqual(
                self.plugin.cookie_file.read_text(), "__csrf=a; MUSIC_U=user=="
            )

    async def test_diagnostic_reports_identity_without_cookie_values(self):
        self.plugin.api.set_api_sources([])
        self.plugin.api.set_cookie("MUSIC_U=private-token")
        self.plugin.api.account = auth.AccountState(
            login_valid=True,
            uid="1001",
            nickname="测试昵称",
            user_name="13800000000",
            membership="VIP",
        )
        with patch.object(self.plugin, "_probe_accounts", new_callable=AsyncMock):
            messages = [text async for text in self.plugin.cmd_diagnose(Event())]
        report = "\n".join(messages)
        self.assertIn("1001", report)
        self.assertIn("测试昵称", report)
        self.assertNotIn("private-token", report)
        self.assertNotIn("13800000000", report)

    async def test_schema_defaults_and_version_match(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertFalse(schema["ncm_api_log_output"]["default"])
        self.assertFalse(schema["ncm_web_cookie_enabled"]["default"])
        self.assertTrue(schema["ncm_api_external_enabled"]["default"])
        self.assertEqual(schema["ncm_api_github_accelerators"]["type"], "list")
        self.assertIn(
            "version: v1.5.0", (root / "metadata.yaml").read_text(encoding="utf-8")
        )
