import asyncio
import base64
import re
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .core.ncm_api import NetEaseAPI, PlayInfo, Song, normalize_quality
from .core.ncm_server import EmbeddedNcmServer
from .core.renderer import CardRenderer
from .core.sender import SongSender

PENDING_EXPIRE = 300  # 选歌缓存有效期（秒）
LYRICS_PER_NODE = 30  # 合并转发每条消息的歌词行数
INVALID_NOTICE_INTERVAL = 600  # 登录失效提示最小间隔（秒），避免刷屏

# 关键词监听触发词
LISTEN_TRIGGERS = ["我要听", "我想听", "想听", "听歌", "点歌", "来一首", "来首", "放一首", "放首", "播放"]
LISTEN_PATTERN = "(" + "|".join(LISTEN_TRIGGERS) + ")"


@register(
    "astrbot_plugin_ncm_player",
    "Kimi",
    "网易云点歌：关键词监听/自然语言点歌、CD 风选歌图、语音/文件/卡片发送、热评卡片、歌词合并转发、扫码登录、内置 NeteaseCloudMusicApi 服务",
    "1.4.5",
)
