import asyncio
import re
import time
import unittest

from aiohttp import web

from .support import account_payload, api_module, auth, serve

NetEaseAPI = api_module.NetEaseAPI


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_qr_and_sms_cookie_survive_real_http_boundary(self):
        """回归：扫码 body 原样拼接会让严格上游解析丢失 MUSIC_U。"""
        raw = "__csrf=csrf; Max-Age=10; Path=/;MUSIC_U=token==; Path=/;MUSIC_A=guest"
        calls = []

        async def handler(request):
            data = await request.json()
            calls.append((request, data))
            if request.path in ("/login/qr/check", "/login/cellphone"):
                return web.json_response(
                    {
                        "code": 803 if request.path.endswith("check") else 200,
                        "cookie": raw,
                    },
                    headers={"Set-Cookie": "NMTID=device; HttpOnly; Path=/"},
                )
            if request.path == "/login/status":
                # 使用上游真实分隔规则校验 wire Cookie，而不只检验字符串含有 token。
                parts = re.split(r";\s+|(?<!\s)\s+$", request.headers.get("Cookie", ""))
                parsed = dict(part.split("=", 1) for part in parts if "=" in part)
                self.assertEqual(parsed["MUSIC_U"], "token==")
                self.assertEqual(data["cookie"]["MUSIC_U"], "token==")
                self.assertNotIn("Path", data["cookie"])
                self.assertNotIn("MUSIC_A", data["cookie"])
                self.assertEqual(request.headers["X-Apicache-Bypass"], "true")
                self.assertNotIn("token", request.query_string)
                return web.json_response(account_payload())
            if request.path == "/vip/info":
                return web.json_response(
                    {
                        "code": 200,
                        "data": {
                            "redplus": {"expireTime": int(time.time() * 1000) + 60000}
                        },
                    }
                )
            return web.json_response({"code": 200})

        async with serve(handler) as base:
            api = NetEaseAPI(ncm_api_base=base, proxy="http://127.0.0.1:1")
            self.addAsyncCleanup(api.close)
            code, cookie = await api.qr_check("fake-key")
            self.assertEqual(code, 803)
            self.assertTrue(auth.has_music_u(cookie))
            api.set_cookie(cookie)
            self.assertTrue(await api.probe_login(force=True))
            self.assertEqual(api.account.uid, "1001")
            self.assertEqual(api.account.membership, "SVIP")
            sms = await api.login_cellphone("13800000000", "1234")
            self.assertEqual(cookie, sms)
            self.assertTrue(all(req.method == "POST" for req, _ in calls))
            self.assertEqual(
                len({req.query["_ncm_nonce"] for req, _ in calls}), len(calls)
            )

    async def test_guest_mismatch_and_network_error_are_distinct(self):
        mode = "valid"

        async def handler(request):
            if request.path == "/vip/info":
                return web.json_response({"code": 500}, status=500)
            if request.path == "/user/detail":
                return web.json_response({"code": 400})
            if mode == "error":
                return web.Response(status=503)
            result = account_payload()
            if mode == "guest":
                result["data"]["account"]["anonimous"] = True
            if mode == "mismatch":
                result["data"]["profile"]["userId"] = "9999"
            return web.json_response(result)

        async with serve(handler) as base:
            api = NetEaseAPI(ncm_api_base=base, cookie="MUSIC_U=valid")
            self.addAsyncCleanup(api.close)
            self.assertTrue(await api.probe_login(force=True))
            self.assertIsNone(api.account.vip)
            for mode, reason in (("guest", "guest"), ("mismatch", "mismatch")):
                self.assertFalse(await api.probe_login(force=True))
                self.assertEqual(api.account.reason, reason)
            mode = "valid"
            self.assertTrue(await api.probe_login(force=True))
            mode = "error"
            self.assertTrue(await api.probe_login(force=True))
            self.assertEqual(api.account.uid, "1001")
            self.assertIn("ClientResponseError", api.account.error)

    async def test_old_probe_cannot_overwrite_new_login(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.path == "/login/status":
                entered.set()
                await release.wait()
                return web.json_response(account_payload("old"))
            return web.json_response({"code": 200, "data": {}})

        async with serve(handler) as base:
            api = NetEaseAPI(ncm_api_base=base, cookie="MUSIC_U=old")
            self.addAsyncCleanup(api.close)
            task = asyncio.create_task(api.probe_login(force=True))
            await asyncio.wait_for(entered.wait(), 2)
            api.set_cookie("MUSIC_U=new")
            api.account = auth.AccountState(login_valid=True, uid="new")
            release.set()
            self.assertFalse(await task)
            self.assertEqual(api.account.uid, "new")

    async def test_partial_membership_response_does_not_erase_identity(self):
        mode = "partial"

        async def handler(request):
            if request.path == "/login/status":
                return web.json_response(
                    account_payload(vip=11 if mode == "partial" else 0)
                )
            if request.path == "/vip/info":
                data = (
                    {"musicPackage": {"expireTime": 0}}
                    if mode != "malformed"
                    else ["bad structure"]
                )
                return web.json_response({"code": 200, "data": data})
            return web.json_response({"code": 400})

        async with serve(handler) as base:
            api = NetEaseAPI(ncm_api_base=base, cookie="MUSIC_U=valid")
            self.addAsyncCleanup(api.close)
            self.assertTrue(await api.probe_login(force=True))
            self.assertTrue(api.account.vip, "单一过期产品不能推翻账号的有效会员字段")
            mode = "expired"
            self.assertTrue(await api.probe_login(force=True))
            self.assertFalse(api.account.vip)
            mode = "malformed"
            self.assertTrue(await api.probe_login(force=True))
            self.assertEqual(api.account.uid, "1001")
            self.assertIsNone(api.account.vip)
            self.assertIn("会员查询失败", api.account.error)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_dual_account_order_download_failure_and_all_levels(self):
        calls = []

        async def handler(request):
            data = await request.json()
            token = data["cookie"].get("MUSIC_U", "")
            if request.path.endswith("/status"):
                return web.json_response(account_payload(token))
            if request.path.endswith("/info"):
                return web.json_response(
                    {
                        "code": 200,
                        "data": {
                            "associator": {
                                "expireTime": 0
                                if token == "primary"
                                else int(time.time() * 1000) + 60000
                            }
                        },
                    }
                )
            if request.path == "/song/url/v1":
                calls.append((request.host, token, data["level"]))
                value = {
                    "url": "http://audio.invalid/track",
                    "fee": 1,
                    "level": "standard",
                    "br": 128000,
                    "freeTrialInfo": {"start": 0, "end": 30}
                    if token == "primary"
                    else "null",
                }
                return web.json_response({"data": [value]})
            return web.json_response({"code": 200})

        async with serve(handler) as inner, serve(handler) as outer:
            api = NetEaseAPI(
                cookie="MUSIC_U=primary",
                web_cookie="MUSIC_U=backup",
                web_cookie_enabled=True,
                meting_api="http://mirror.invalid",
            )
            api.set_api_sources([("内置", inner), ("外部", outer)])
            self.addAsyncCleanup(api.close)
            trace = api_module.PlaybackTrace()
            iterator = api.iter_play_info(1, "hires", trace)
            play = await anext(iterator)
            self.assertEqual(play.credential, "web")
            self.assertEqual(play.account_uid, "backup")
            self.assertEqual(play.quality_str, "标准 128k")
            self.assertTrue(trace.trial)
            expected = ["hires", "lossless", "exhigh", "higher", "standard"]
            self.assertEqual([item[2] for item in calls[:5]], expected)
            self.assertEqual([item[2] for item in calls[5:10]], expected)
            self.assertEqual([item[1] for item in calls[:10]], ["primary"] * 10)
            # 调用方下载失败后继续迭代，必须进入下一服务/镜像，而非结束链路。
            trace.record(play.source_label, "下载失败")
            api.PLAY_URL = "http://127.0.0.1:1/unavailable"
            rest = [candidate async for candidate in iterator]
            self.assertEqual([item.source for item in rest], ["meting", "outer"])
            self.assertIn("下载失败", rest[-1].fallback_reason)

    async def test_cookie_sent_on_direct_web_and_switches(self):
        async def handler(request):
            self.assertIn("MUSIC_U=web-token", request.headers.get("Cookie", ""))
            return web.json_response(
                {
                    "data": [
                        {
                            "url": "https://audio.invalid/full",
                            "br": 320000,
                            "freeTrialInfo": None,
                        }
                    ]
                }
            )

        async with serve(handler) as base:
            api = NetEaseAPI(
                web_cookie="MUSIC_U=web-token",
                web_cookie_enabled=True,
                meting_enabled=False,
                outer_enabled=False,
            )
            self.addAsyncCleanup(api.close)
            api.PLAY_URL = base
            # 没有 API 时登录诊断直连网易云；此测试只验证本地音源请求边界。
            api.web_account = auth.AccountState(
                login_valid=True, uid="web", checked_at=time.monotonic()
            )
            result = [play async for play in api.iter_play_info(1, "standard")]
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].source, "web")
            self.assertEqual(result[0].quality_str, "320kbps")
            api.web_enabled = False
            self.assertEqual(
                [play async for play in api.iter_play_info(1, "standard")], []
            )

    async def test_parallel_playback_traces_are_independent(self):
        async def handler(request):
            body = await request.json()
            return web.json_response(
                {
                    "data": [
                        {
                            "url": "https://audio.invalid/full",
                            "freeTrialInfo": {"end": 30} if body["id"] == 1 else None,
                        }
                    ]
                }
            )

        async with serve(handler) as base:
            api = NetEaseAPI(ncm_api_base=base, web_enabled=False)
            self.addAsyncCleanup(api.close)
            traces = [api_module.PlaybackTrace(), api_module.PlaybackTrace()]

            async def pick(song, trace):
                return await anext(api.iter_play_info(song, "standard", trace))

            first, second = await asyncio.gather(pick(1, traces[0]), pick(2, traces[1]))
            self.assertEqual(first.source, "outer")
            self.assertEqual(second.source, "ncm")
            self.assertTrue(traces[0].trial)
            self.assertFalse(traces[1].trial)
            self.assertEqual(second.fallback_reason, "")
