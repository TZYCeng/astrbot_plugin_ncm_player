"""跨系统映射与 OneBot 发送载荷回归测试，不依赖本机操作系统。"""

import base64
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from . import support as _support

assert _support

from core.sender import SongSender, file_uri, to_guest_path  # noqa: E402


class GuestPathTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"PATH_CONVERT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_preserves_paths_without_mapping(self):
        for path in (
            "/AstrBot/data/cache/x.mp3",
            "/opt/data/x.mp3",
            r"C:\AstrBot\data\x.mp3",
        ):
            for style in ("", "auto"):
                with self.subTest(path=path, style=style):
                    self.assertEqual(to_guest_path(path, style), path)

    def test_deployment_mapping_matrix(self):
        cases = [
            (
                "/AstrBot/data",
                "/shared",
                "/AstrBot/data/cache/x.mp3",
                "/shared/cache/x.mp3",
                "linux",
            ),
            (
                "/AstrBot/data",
                r"D:\shared",
                "/AstrBot/data/cache/x.mp3",
                r"D:\shared\cache\x.mp3",
                "windows",
            ),
            (
                r"C:\AstrBot\data",
                "/shared",
                r"c:\astrbot\DATA\cache\x.mp3",
                "/shared/cache/x.mp3",
                "linux",
            ),
            (
                r"C:\AstrBot\data",
                r"D:\shared",
                "C:/AstrBot/data/cache/x.mp3",
                r"D:\shared\cache\x.mp3",
                "windows",
            ),
            (
                "/AstrBot/data",
                r"\\server\music",
                "/AstrBot/data/x.mp3",
                r"\\server\music\x.mp3",
                "windows",
            ),
        ]
        for src, dst, path, expected, style in cases:
            with self.subTest(src=src, dst=dst):
                with mock.patch.dict(os.environ, {"PATH_CONVERT": f"{src}=>{dst}"}):
                    self.assertEqual(to_guest_path(path), expected)
                    self.assertEqual(to_guest_path(path, style), expected)

    def test_longest_prefix_and_boundary(self):
        with mock.patch.dict(
            os.environ,
            {"PATH_CONVERT": "/AstrBot=>/general;/AstrBot/data/=>/specific/"},
        ):
            self.assertEqual(to_guest_path("/AstrBot/data/x.mp3"), "/specific/x.mp3")
            self.assertEqual(
                to_guest_path("/AstrBot/database/x.mp3"), "/general/database/x.mp3"
            )
            self.assertEqual(to_guest_path("/AstrBot/data"), "/specific")
            self.assertEqual(
                to_guest_path("/astrbot/data/x.mp3"), "/astrbot/data/x.mp3"
            )

    def test_root_and_normalization(self):
        with mock.patch.dict(os.environ, {"PATH_CONVERT": "/=>/shared"}):
            self.assertEqual(to_guest_path("/cache/x.mp3"), "/shared/cache/x.mp3")
        with mock.patch.dict(os.environ, {"PATH_CONVERT": "/AstrBot/data=>/shared"}):
            self.assertEqual(
                to_guest_path("/AstrBot/data/../secret/x.mp3"),
                "/AstrBot/data/../secret/x.mp3",
            )

    def test_explicit_style_requires_actual_mapping(self):
        self.assertEqual(
            to_guest_path("/AstrBot/data/x.mp3", "linux"), "/AstrBot/data/x.mp3"
        )
        for path, style in (
            ("/AstrBot/data/x.mp3", "windows"),
            (r"C:\data\x.mp3", "linux"),
        ):
            with self.assertRaisesRegex(ValueError, "PATH_CONVERT"):
                to_guest_path(path, style)

    def test_invalid_mapping_rejected(self):
        for mapping in (
            "/data",
            "/data=>relative",
            "relative=>/data",
            "/data=>C:relative",
        ):
            with self.subTest(mapping=mapping):
                with mock.patch.dict(os.environ, {"PATH_CONVERT": mapping}):
                    with self.assertRaises(ValueError):
                        to_guest_path("/data/x.mp3")

    def test_uri_encoding_and_unc(self):
        cases = [
            (r"C:\cache\A B+#%.mp3", "file:///C:/cache/A%20B%2B%23%25.mp3"),
            ("/cache/中文.mp3", "file:///cache/%E4%B8%AD%E6%96%87.mp3"),
            (r"\\server\share\A B.mp3", "file://server/share/A%20B.mp3"),
            ("/cache/A B.mp3", "file:///cache/A%20B.mp3"),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(file_uri(path), expected)
        for path in ("relative.mp3", "C:relative.mp3"):
            with self.assertRaises(ValueError):
                file_uri(path)


class SenderPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_record_and_file_share_target_path(self):
        for group in ("", "456"):
            for destination in ("/shared", r"D:\shared"):
                with self.subTest(group=group, destination=destination):
                    event = _support.Event()
                    event.get_group_id = lambda: group
                    event.bot = mock.AsyncMock()
                    sender = SongSender({"load_mode": "file"})
                    with mock.patch.object(
                        Path, "resolve", return_value=Path("/source/cache/x.mp3")
                    ):
                        # str(Path(...)) is native; use an explicit POSIX string for Docker simulation.
                        with mock.patch("core.sender.Path") as path_type:
                            path_type.return_value.resolve.return_value = (
                                "/source/cache/x.mp3"
                            )
                            with mock.patch.dict(
                                os.environ, {"PATH_CONVERT": f"/source=>{destination}"}
                            ):
                                target = to_guest_path("/source/cache/x.mp3")
                                await sender._try_record(
                                    event, SimpleNamespace(url="unused"), "local.mp3"
                                )
                                await sender._try_file(
                                    event,
                                    SimpleNamespace(name="song", artists="artist"),
                                    "local.mp3",
                                    "mp3",
                                )
                    send = (
                        event.bot.send_group_msg
                        if group
                        else event.bot.send_private_msg
                    )
                    upload = (
                        event.bot.upload_group_file
                        if group
                        else event.bot.upload_private_file
                    )
                    self.assertEqual(
                        send.call_args.kwargs["message"][0]["data"]["file"],
                        file_uri(target),
                    )
                    self.assertEqual(upload.call_args.kwargs["file"], target)

    async def test_base64_uses_local_bytes_without_mapping(self):
        event = _support.Event()
        event.bot = mock.AsyncMock()
        payload = b"test audio\x00\xff"
        with mock.patch.object(Path, "read_bytes", return_value=payload):
            with mock.patch(
                "core.sender.to_guest_path", side_effect=AssertionError("must not map")
            ):
                await SongSender({"load_mode": "base64"})._try_record(
                    event, SimpleNamespace(url="unused"), "local.mp3"
                )
        field = event.bot.send_private_msg.call_args.kwargs["message"][0]["data"][
            "file"
        ]
        self.assertEqual(base64.b64decode(field.removeprefix("base64://")), payload)

    async def test_url_needs_no_local_file(self):
        event = _support.Event()
        event.bot = mock.AsyncMock()
        url = "https://example.test/audio.mp3"
        await SongSender({"load_mode": "url"})._try_record(
            event, SimpleNamespace(url=url), None
        )
        self.assertEqual(
            event.bot.send_private_msg.call_args.kwargs["message"][0]["data"]["file"],
            url,
        )


if __name__ == "__main__":
    unittest.main()
