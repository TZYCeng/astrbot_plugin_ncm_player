"""用真实 aiohttp 服务测试网络边界；仅替代未安装的 AstrBot 宿主接口。"""

import importlib
import logging
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import web


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _decorator(*args, **kwargs):
    return lambda target: target


class Config(dict):
    def save_config(self):
        self.saved = dict(self)


class Component:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs

    @classmethod
    def fromFileSystem(cls, value):
        return cls(value)

    fromURL = fromFileSystem


class Event:
    unified_msg_origin = "test:private:123"
    message_str = ""

    def __init__(self):
        self.messages = []

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    async def send(self, result):
        self.messages.append(result)

    def get_sender_id(self):
        return "123"

    def get_group_id(self):
        return ""

    def is_private_chat(self):
        return True

    def is_admin(self):
        return True


class Star:
    def __init__(self, context):
        self.context = context


class StarTools:
    @staticmethod
    def get_data_dir():
        raise AssertionError("测试必须显式指定插件数据目录")


_module("astrbot")
_module("astrbot.api", logger=logging.getLogger("ncm-tests"), AstrBotConfig=Config)
_module(
    "astrbot.api.event",
    AstrMessageEvent=Event,
    filter=types.SimpleNamespace(
        command=_decorator,
        permission_type=_decorator,
        llm_tool=_decorator,
        regex=_decorator,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
    ),
)
_module(
    "astrbot.api.message_components",
    **dict.fromkeys(
        ["Image", "Plain", "Record", "Node", "Nodes"],
        Component,
    ),
)
_module(
    "astrbot.api.star",
    Star=Star,
    StarTools=StarTools,
    Context=object,
    register=_decorator,
)
_module("astrbot.core")
_module("astrbot.core.utils")
_module(
    "astrbot.core.utils.session_waiter",
    SessionController=object,
    session_waiter=_decorator,
)
_module("_ncm_test_plugin", __path__=[str(Path(__file__).resolve().parents[1])])

auth = importlib.import_module("_ncm_test_plugin.core.auth")
api_module = importlib.import_module("_ncm_test_plugin.core.ncm_api")
server_module = importlib.import_module("_ncm_test_plugin.core.ncm_server")
main_module = importlib.import_module("_ncm_test_plugin.main")


@asynccontextmanager
async def serve(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def account_payload(uid="1001", vip=0):
    return {
        "data": {
            "code": 200,
            "account": {"id": uid, "userName": "test-name"},
            "profile": {"userId": uid, "nickname": "测试账号", "vipType": vip},
        }
    }
