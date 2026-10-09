"""网易云请求、独立账号快照及可继续迭代的多级音源链路。"""

import asyncio
import ipaddress
import json
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import logger

from .auth import (
    AccountState,
    has_music_u,
    membership_status,
    normalize_cookie,
    parse_cookie,
    response_cookie,
)

# 音质档位 -> (显示名, 官方接口码率, ncm_api level)
QUALITY_LEVELS: dict[str, tuple[str, int, str]] = {
    "standard": ("标准 128k", 128000, "standard"),
    "higher": ("较高 192k", 192000, "higher"),
    "exhigh": ("极高 320k", 320000, "exhigh"),
    "lossless": ("无损 FLAC", 999000, "lossless"),
    "hires": ("高清臻音 Hi-Res", 999000, "hires"),
    "jymaster": ("超清母带", 999000, "jymaster"),
}
# 从用户选择的档位向下回退，不请求高于用户选择的档位。
LEVEL_FALLBACK = list(reversed(QUALITY_LEVELS))

# 配置值别名：中文档位名 / 旧版英文 key -> 内部 key
QUALITY_ALIASES: dict[str, str] = {
    "标准 128k": "standard",
    "较高 192k": "higher",
    "极高 320k": "exhigh",
    "无损 FLAC": "lossless",
    "高清臻音 Hi-Res": "hires",
    "超清母带": "jymaster",
    **{k: k for k in QUALITY_LEVELS},
}


def normalize_quality(value: str) -> str:
    """把配置里的音质值（中文档位名或旧版英文 key）归一化为内部 key"""
    return QUALITY_ALIASES.get(str(value).strip(), "exhigh")


@dataclass
class Song:
    id: int
    name: str
    artists: str
    album: str = ""
    duration: int = 0  # 毫秒
    pic_url: str = ""

    @property
    def duration_str(self) -> str:
        sec = max(0, self.duration // 1000)
        return f"{sec // 60:02d}:{sec % 60:02d}"

    @property
    def page_url(self) -> str:
        return f"https://music.163.com/song?id={self.id}"


@dataclass
class PlayInfo:
    url: str
    br: int = 0  # 实际码率，0 表示未知
    size: int = 0  # 字节，0 表示未知
    ext: str = "mp3"
    level: str = ""  # 音质档位 key，空表示未知来源
    source: str = ""  # 来源：ncm / web / meting / outer
    fee: int = 0  # 1=VIP 歌曲 4=付费专辑，0/8=免费（用于卡片 VIP 标记）
    service: str = ""
    credential: str = ""
    account_uid: str = ""
    authenticated: bool = False
    fallback_reason: str = ""

    @property
    def source_label(self) -> str:
        if self.source == "ncm":
            account = "网页 Cookie" if self.credential == "web" else "API 账号"
            return f"{self.service} / {account}"
        return {"web": "官方网页", "meting": "Meting 镜像", "outer": "官方外链"}.get(
            self.source, self.source
        )

    @property
    def is_vip_song(self) -> bool:
        return self.fee in (1, 4)

    @property
    def size_mb(self) -> float:
        return self.size / 1024 / 1024 if self.size else 0.0

    @property
    def quality_str(self) -> str:
        if self.level in QUALITY_LEVELS:
            return QUALITY_LEVELS[self.level][0]
        if self.source == "meting":
            return "Meting 镜像"
        if self.source == "outer":
            return "官方外链"
        if self.ext == "flac":
            return "无损"
        if self.br > 0:
            return f"{self.br // 1000}kbps"
        return "在线"


@dataclass
class PlaybackTrace:
    """每次点歌独有；迭代器暂停下载时不会被其他点歌请求覆盖。"""

    failures: list[str] = field(default_factory=list)
    trial: bool = False
    fee: int = 0

    def record(self, source: str, reason: str):
        message = f"{source}: {reason}"
        if message not in self.failures:
            self.failures.append(message)

    @property
    def summary(self) -> str:
        return "；".join(self.failures)


@dataclass
class Comment:
    content: str
    nickname: str
    liked_count: int = 0
    avatar_url: str = ""


_LRC_TAG = re.compile(r"\[[^\]]*\]")


class NetEaseAPI:
    SEARCH_URL = "https://music.163.com/api/search/get/web"
    DETAIL_URL = "https://music.163.com/api/song/detail/"
    PLAY_URL = "https://music.163.com/api/song/enhance/player/url"
    OUTER_URL = "https://music.163.com/song/media/outer/url"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://music.163.com/",
    }

    def __init__(
        self,
        proxy: str = "",
        ncm_api_base: str = "",
        meting_api: str = "",
        cookie: str = "",
        web_cookie: str = "",
        web_cookie_enabled: bool = False,
        web_enabled: bool = True,
        meting_enabled: bool = True,
        outer_enabled: bool = True,
    ):
        self.proxy = proxy or None
        self.api_sources = (
            [("外部 API", ncm_api_base.rstrip("/"))] if ncm_api_base else []
        )
        self.meting_api = meting_api or ""
        self.cookie = normalize_cookie(cookie)
        self.web_cookie = normalize_cookie(web_cookie) if web_cookie_enabled else ""
        self.web_cookie_enabled = web_cookie_enabled
        self.web_enabled = web_enabled
        self.meting_enabled = meting_enabled
        self.outer_enabled = outer_enabled
        self.account = AccountState()
        self.web_account = AccountState()
        self._generation = 0
        self._probe_locks = {"api": asyncio.Lock(), "web": asyncio.Lock()}
        # DummyCookieJar：禁用会话自动存/发 Cookie。
        # 否则登录接口 Set-Cookie 会被 CookieJar 记住，与手动传入的 Cookie 头叠加，
        # 可能出现重复/过期 MUSIC_U，导致会员鉴权时好时坏（VIP 歌偶尔变 30 秒试听）。
        self.session = aiohttp.ClientSession(
            headers=self.HEADERS,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=15),
        )

    async def close(self):
        await self.session.close()

    @property
    def ncm_api_base(self) -> str:
        return self.api_sources[0][1] if self.api_sources else ""

    def set_api_sources(self, sources: list[tuple[str, str]]):
        """服务切换使旧复核失效；各播放请求仍持有自己的服务/凭据快照。"""
        self.api_sources = list(
            dict.fromkeys((name, url.rstrip("/")) for name, url in sources if url)
        )
        self._generation += 1
        self.account = AccountState()
        self.web_account = AccountState()

    def set_cookie(self, cookie: str):
        self.cookie = normalize_cookie(cookie)
        self._generation += 1
        self.account = AccountState()

    def adopt_login(self, candidate: "NetEaseAPI"):
        self.set_cookie(candidate.cookie)
        self.account = candidate.account

    def _proxy_for(self, url: str):
        """本机 API 不经过用户为公网网易云配置的 HTTP 代理。"""
        host = urlsplit(url).hostname or ""
        if host.lower() == "localhost":
            return None
        try:
            if ipaddress.ip_address(host).is_loopback:
                return None
        except ValueError:
            pass
        return self.proxy

    def _auth_headers(self, cookie: str | None = None) -> dict:
        h = dict(self.HEADERS)
        value = self.cookie if cookie is None else cookie
        h["Cookie"] = normalize_cookie(value)
        # 上游 apicache 仅认专用 bypass 头；普通 no-cache 只供中间代理参考。
        h["X-Apicache-Bypass"] = "true"
        h["Cache-Control"] = "no-cache"
        h["Pragma"] = "no-cache"
        return h

    async def _api_request(
        self, path: str, *, base: str = "", cookie: str | None = None, **params
    ):
        """凭据在 POST 对象中传递，绕过上游严格的 Cookie 字符串解析。

        URL 只放随机缓存键；避免凭据出现在访问日志，同时隔离忽略请求体的缓存。
        """
        url = f"{base or self.ncm_api_base}{path}"
        value = self.cookie if cookie is None else cookie
        async with self.session.post(
            url,
            json={**params, "cookie": parse_cookie(value)},
            params={"timestamp": self._ts(), "_ncm_nonce": uuid.uuid4().hex},
            headers=self._auth_headers(value),
            proxy=self._proxy_for(url),
        ) as resp:
            resp.raise_for_status()
            result = await resp.json(content_type=None)
            if not isinstance(result, dict):
                raise ValueError("API 未返回 JSON 对象")
            return result

    async def _get(
        self, url: str, auth: bool = False, cookie: str | None = None, **kwargs
    ):
        headers = self._auth_headers(cookie) if auth else None
        async with self.session.get(
            url, proxy=self._proxy_for(url), headers=headers, **kwargs
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ---------- 搜索 / 详情 ----------

    async def search(self, keyword: str, limit: int = 5) -> list[Song]:
        """搜索歌曲，并批量补全封面、专辑信息"""
        async with self.session.post(
            self.SEARCH_URL,
            data={"s": keyword, "type": 1, "limit": limit, "offset": 0},
            proxy=self.proxy,
        ) as resp:
            resp.raise_for_status()
            result = await resp.json(content_type=None)

        raw_songs = (result.get("result") or {}).get("songs") or []
        songs = [
            Song(
                id=s["id"],
                name=s.get("name", "未知歌曲"),
                artists="、".join(a.get("name", "") for a in s.get("artists", [])),
                album=(s.get("album") or {}).get("name", ""),
                duration=s.get("duration", 0),
            )
            for s in raw_songs[:limit]
        ]
        if not songs:
            return []

        # 批量取封面
        try:
            ids = ",".join(str(s.id) for s in songs)
            detail = await self._get(self.DETAIL_URL, params={"ids": f"[{ids}]"})
            pic_map = {
                d["id"]: (d.get("album") or {}).get("picUrl", "")
                for d in detail.get("songs", [])
            }
            for s in songs:
                s.pic_url = pic_map.get(s.id, "")
        except Exception as e:
            logger.warning(f"[ncm_player] 获取封面失败，将使用无封面渲染: {e}")
        return songs

    # ---------- 播放地址 ----------

    @staticmethod
    def _is_trial(d: dict) -> bool:
        """上游可能返回 null 或字符串 'null'，只有实质试听信息才拒绝。"""
        value = d.get("freeTrialInfo")
        if isinstance(value, str):
            if value.strip().lower() in ("", "null", "none", "false", "0"):
                return False
            try:
                value = json.loads(value)
            except ValueError:
                return True
        return bool(value)

    async def get_play_info(self, song_id: int, quality: str) -> PlayInfo | None:
        """兼容仅需链接的调用；下载调用应使用 iter_play_info 继续回退。"""
        async for info in self.iter_play_info(song_id, quality):
            return info
        return None

    @staticmethod
    def _play_info(
        data: dict, source: str, trace: PlaybackTrace, **kwargs
    ) -> PlayInfo | None:
        label = kwargs.get("service") or {"web": "官方网页"}.get(source, source)
        if kwargs.get("service") and kwargs.get("credential"):
            label += f"/{kwargs['credential']}"
        trace.fee = int(data.get("fee") or trace.fee)
        if NetEaseAPI._is_trial(data):
            trace.trial = True
            trace.record(label, "仅试听")
            return None
        url = data.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
            trace.record(label, "无可用音频")
            return None
        ext = str(data.get("type") or "mp3").lower()
        # 音频类型来自服务响应，只允许文件扩展名，不能进入任意路径。
        if ext not in ("mp3", "flac", "m4a", "aac", "ogg", "wav"):
            ext = "mp3"
        return PlayInfo(
            url=url,
            br=int(data.get("br") or 0),
            size=int(data.get("size") or 0),
            ext=ext,
            level=str(data.get("level") or ""),
            source=source,
            fee=trace.fee,
            fallback_reason=trace.summary,
            **kwargs,
        )

    async def iter_play_info(
        self, song_id: int, quality: str, trace: PlaybackTrace | None = None
    ):
        """API 账号(内→外) → 网页 Cookie(内→外→直连) → 镜像 → 外链。

        每次 yield 后调用方可下载/探测；失败后继续迭代，避免取得坏链接就结束回退。
        Cookie 和服务列表在首次请求前快照，切换账号不会在一首歌中混用凭据。
        """
        trace = trace if trace is not None else PlaybackTrace()
        sources = tuple(self.api_sources)
        cookie, web_cookie = self.cookie, self.web_cookie
        generation = self._generation
        quality = normalize_quality(quality)
        levels = LEVEL_FALLBACK[LEVEL_FALLBACK.index(quality) :]
        seen = set()
        credentials = [("api", cookie)]
        if self.web_enabled and self.web_cookie_enabled and has_music_u(web_cookie):
            credentials.append(("web", web_cookie))

        for credential, value in credentials:
            if value and generation == self._generation:
                await self.probe_login(credential=credential)
            account = self.web_account if credential == "web" else self.account
            if generation != self._generation:
                account = AccountState()
            for service, base in sources:
                label = f"{service}/{credential}"
                for level in levels:
                    try:
                        result = await self._api_request(
                            "/song/url/v1",
                            base=base,
                            cookie=value,
                            id=song_id,
                            level=level,
                        )
                        data = (result.get("data") or [{}])[0]
                        info = self._play_info(
                            data,
                            "ncm",
                            trace,
                            service=service,
                            credential=credential,
                            account_uid=account.uid,
                            authenticated=account.login_valid is True,
                        )
                    except Exception as exc:
                        trace.record(label, f"请求失败({type(exc).__name__})")
                        # 网络/服务错误不因换音质而恢复，直接切换服务。
                        break
                    if info and info.url not in seen:
                        seen.add(info.url)
                        yield info

        if self.web_enabled:
            value = web_cookie if self.web_cookie_enabled else ""
            account = (
                self.web_account if generation == self._generation else AccountState()
            )
            for br in dict.fromkeys(QUALITY_LEVELS[level][1] for level in levels):
                try:
                    result = await self._get(
                        self.PLAY_URL,
                        auth=True,
                        cookie=value,
                        params={
                            "ids": f"[{song_id}]",
                            "br": br,
                            "timestamp": self._ts(),
                        },
                    )
                    data = (result.get("data") or [{}])[0]
                    info = self._play_info(
                        data,
                        "web",
                        trace,
                        credential="web" if value else "",
                        account_uid=account.uid,
                        authenticated=account.login_valid is True,
                    )
                except Exception as exc:
                    trace.record("官方网页", f"请求失败({type(exc).__name__})")
                    break
                if info and info.url not in seen:
                    seen.add(info.url)
                    yield info

        if self.meting_enabled and self.meting_api:
            sep = "&" if "?" in self.meting_api else "?"
            yield PlayInfo(
                url=f"{self.meting_api}{sep}server=netease&type=url&id={song_id}",
                source="meting",
                fee=trace.fee,
                fallback_reason=trace.summary,
            )
        if self.outer_enabled:
            yield PlayInfo(
                url=f"{self.OUTER_URL}?id={song_id}.mp3",
                source="outer",
                fee=trace.fee,
                fallback_reason=trace.summary,
            )

    # ---------- 热评 / 歌词 ----------

    async def fetch_comment(self, song_id: int) -> Comment | None:
        """取一条最热门评论"""
        try:
            result = await self._get(
                f"https://music.163.com/api/v1/resource/comments/R_SO_4_{song_id}",
                params={"limit": 5},
            )
            hot = result.get("hotComments") or []
            if not hot:
                return None
            c = max(hot, key=lambda x: x.get("likedCount", 0))
            user = c.get("user") or {}
            return Comment(
                content=c.get("content", "").strip(),
                nickname=user.get("nickname", "网易云用户"),
                liked_count=c.get("likedCount", 0),
                avatar_url=user.get("avatarUrl", ""),
            )
        except Exception as e:
            logger.warning(f"[ncm_player] 获取热评失败: {e}")
            return None

    async def fetch_lyric(self, song_id: int) -> list[str] | None:
        """取整首歌词，去除时间轴标签，返回行列表。纯音乐/无歌词返回 None"""
        try:
            result = await self._get(
                "https://music.163.com/api/song/lyric",
                params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
            )
            if result.get("nolyric") or result.get("uncollected"):
                return None
            lrc = (result.get("lrc") or {}).get("lyric") or ""
            lines = []
            for raw in lrc.splitlines():
                text = _LRC_TAG.sub("", raw).strip()
                if text:
                    lines.append(text)
            return lines or None
        except Exception as e:
            logger.warning(f"[ncm_player] 获取歌词失败: {e}")
            return None

    # ---------- 二维码登录（依赖 NeteaseCloudMusicApi 服务） ----------

    @staticmethod
    def _ts() -> int:
        """毫秒时间戳。服务端 apicache 以完整 URL 为缓存键，
        旧版固定 timestamp=0 会导致扫码状态被缓存，迟迟查不到登录成功"""
        return int(time.time() * 1000)

    async def qr_key(self, base: str = "") -> str:
        result = await self._api_request("/login/qr/key", base=base, cookie="")
        return (result.get("data") or {})["unikey"]

    async def qr_create(self, key: str, base: str = "") -> str:
        """返回二维码图片的 base64 data-uri"""
        result = await self._api_request(
            "/login/qr/create", base=base, cookie="", key=key, qrimg="true"
        )
        return (result.get("data") or {})["qrimg"]

    async def qr_check(self, key: str, base: str = "") -> tuple[int, str]:
        """轮询扫码状态。返回 (code, cookie)。800 过期 801 等待 802 待确认 803 成功。

        只在 803 时提取新 Cookie；响应体优先，补充 Set-Cookie 中缺少的字段，
        避免登录成功却拿到空 cookie（表现为重启/刷新后"掉登录"）。
        """
        url = f"{base or self.ncm_api_base}/login/qr/check"
        async with self.session.post(
            url,
            proxy=self._proxy_for(url),
            headers=self._auth_headers(""),
            json={"key": key, "cookie": {}},
            params={"timestamp": self._ts(), "_ncm_nonce": uuid.uuid4().hex},
        ) as resp:
            resp.raise_for_status()
            result = await resp.json(content_type=None)
            code = int(result.get("code", 0))
            if code != 803:
                return code, ""
            return code, response_cookie(
                result.get("cookie", ""), resp.headers.getall("Set-Cookie", [])
            )

    async def send_captcha(
        self, phone: str, countrycode: str = "86", base: str = ""
    ) -> None:
        """发送短信；请求和异常中的敏感参数不得进入日志或聊天。"""
        try:
            result = await self._api_request(
                "/captcha/sent", base=base, cookie="", phone=phone, ctcode=countrycode
            )
            if result.get("code") != 200:
                raise ValueError(
                    "验证码发送未成功；可能受到风控，请稍后在官方客户端确认"
                )
        except Exception:
            raise ValueError(
                "验证码发送失败或受到风控；请检查服务或稍后在官方客户端确认"
            ) from None

    async def login_cellphone(
        self, phone: str, captcha: str, countrycode: str = "86", base: str = ""
    ) -> str:
        """返回新的认证 Cookie；仅采用本次响应，不使用已有会话 Cookie。"""
        try:
            url = f"{base or self.ncm_api_base}/login/cellphone"
            async with self.session.post(
                url,
                proxy=self._proxy_for(url),
                headers=self._auth_headers(""),
                json={
                    "phone": phone,
                    "countrycode": countrycode,
                    "captcha": captcha,
                    "cookie": {},
                },
                params={"timestamp": self._ts(), "_ncm_nonce": uuid.uuid4().hex},
            ) as resp:
                resp.raise_for_status()
                result = await resp.json(content_type=None)
                if result.get("code") != 200:
                    raise ValueError(
                        "验证码登录未成功；请核对验证码，若受风控请停止重试"
                    )
                cookie = response_cookie(
                    result.get("cookie", ""), resp.headers.getall("Set-Cookie", [])
                )
                if not has_music_u(cookie):
                    raise ValueError("服务未返回可用的新认证 Cookie；原登录态未更改")
                return cookie
        except Exception:
            raise ValueError(
                "验证码登录请求失败或受到风控；请检查服务或稍后重试"
            ) from None

    # 登录态复核防抖间隔（秒）：避免每首 VIP 歌都重复打 /login/status
    LOGIN_CHECK_INTERVAL = 60

    async def _probe_account(
        self, cookie: str, service: str, base: str
    ) -> AccountState:
        async def request(path: str, direct: str, **params):
            if base:
                return await self._api_request(path, base=base, cookie=cookie, **params)
            return await self._get(
                f"https://music.163.com{direct}",
                auth=True,
                cookie=cookie,
                params={**params, "timestamp": self._ts()},
            )

        result = await request("/login/status", "/api/nuser/account/get")
        data = result.get("data") or result
        account, profile = data.get("account") or {}, data.get("profile") or {}
        reason = ""
        code = data.get("code", result.get("code", 200))
        uid = str(account.get("id") or "")
        profile_uid = str(profile.get("userId") or profile.get("id") or "")
        if int(code) != 200:
            # 风控/服务错误不是 cookie 已过期的直接证据。
            if int(code) not in (301, 401):
                raise ValueError(f"账号接口 code={code}")
            reason = "bad_code"
        elif account.get("anonimous") or account.get("anonymous"):
            reason = "guest"
        elif not uid:
            reason = "no_account"
        elif profile_uid and uid != profile_uid:
            reason = "mismatch"
        if reason:
            return AccountState(
                login_valid=False,
                reason=reason,
                service=service,
                checked_at=time.monotonic(),
            )

        profiles = [account, profile]
        membership, error = {}, ""
        try:
            membership = await request(
                "/vip/info",
                "/api/music-vip-membership/front/vip/info",
                uid=uid,
                userId=uid,
            )
            if membership.get("code") != 200:
                error = "会员接口未返回成功状态"
            member_uid = str((membership.get("data") or {}).get("userId") or "")
            if member_uid and member_uid != uid:
                membership = {}
                error = "会员接口 UID 不一致"
        except Exception as exc:
            membership = {}
            error = f"会员查询失败({type(exc).__name__})"
        vip, label = membership_status(membership, profiles)
        if vip is None and base:
            try:
                detail = await request("/user/detail", "", uid=uid)
                p = detail.get("profile") or {}
                if detail.get("code") == 200 and str(p.get("userId") or "") == uid:
                    profiles.append(p)
                    vip, label = membership_status(membership, profiles)
            except Exception:
                pass
        return AccountState(
            login_valid=True,
            vip=vip,
            membership=label,
            uid=profile_uid or uid,
            nickname=str(profile.get("nickname") or ""),
            user_name=str(account.get("userName") or ""),
            error=error,
            service=service,
            checked_at=time.monotonic(),
        )

    async def probe_login(self, force: bool = False, credential: str = "api") -> bool:
        """防抖复核；只有快照代数仍一致才能发布结果，旧请求不能覆盖新登录。"""
        attr = "web_account" if credential == "web" else "account"
        async with self._probe_locks[credential]:
            previous = getattr(self, attr)
            if (
                not force
                and time.monotonic() - previous.checked_at < self.LOGIN_CHECK_INTERVAL
            ):
                return previous.login_valid is True
            generation = self._generation
            cookie = self.web_cookie if credential == "web" else self.cookie
            if not has_music_u(cookie):
                setattr(
                    self,
                    attr,
                    AccountState(
                        login_valid=False,
                        reason="no_credential",
                        checked_at=time.monotonic(),
                    ),
                )
                return False
            sources = tuple(self.api_sources) or (("官方网页", ""),)
            checked, errors = [], []
            for service, base in sources:
                try:
                    state = await self._probe_account(cookie, service, base)
                    checked.append(state)
                    if state.login_valid:
                        break
                except Exception as exc:
                    errors.append(f"{service}: {type(exc).__name__}")
            if generation != self._generation:
                return False
            valid = next((state for state in checked if state.login_valid), None)
            if valid:
                state = valid
            elif errors:
                state = replace(
                    previous, error="；".join(errors), checked_at=time.monotonic()
                )
            else:
                state = checked[-1]
            setattr(self, attr, state)
            return state.login_valid is True

    async def check_login(self) -> bool:
        """校验当前 cookie 是否仍有效（用于启动时提示登录状态）"""
        return await self.probe_login(force=True)

    # ---------- 下载 ----------

    async def check_audio_url(self, url: str) -> bool:
        """URL 发送模式只读少量响应，不在本机下载整首音频。"""
        try:
            async with self.session.get(
                url,
                proxy=self._proxy_for(url),
                headers={"Range": "bytes=0-1023"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                self._check_audio_type(resp)
                return bool(await resp.content.read(1024))
        except Exception:
            return False

    @staticmethod
    def _check_audio_type(resp):
        ctype = (resp.content_type or "").lower()
        if not (ctype.startswith("audio/") or ctype == "application/octet-stream"):
            raise ValueError(f"返回的不是音频(content-type={ctype})")

    async def fetch_bytes(self, url: str, timeout: int = 10) -> bytes | None:
        """下载小文件（封面图、头像等）"""
        try:
            async with self.session.get(
                url, proxy=self.proxy, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                resp.raise_for_status()
                return await resp.read()
        except Exception as e:
            logger.warning(f"[ncm_player] 下载资源失败 {url}: {e}")
            return None

    async def download(self, url: str, dest: str, max_mb: int, timeout: int) -> int:
        """流式下载音频到本地，超限即中止，失败自动重试一次。返回实际字节数。

        timeout 语义为「停滞超时」（sock_read）：只要数据持续到达就不判超时，
        避免旧版 total 总超时把网速慢但正常下载的大文件（无损/母带）掐死。
        """
        limit = max_mb * 1024 * 1024
        last_err: Exception | None = None
        for attempt in range(2):
            written = 0
            try:
                async with self.session.get(
                    url,
                    proxy=self._proxy_for(url),
                    timeout=aiohttp.ClientTimeout(
                        total=None, connect=10, sock_connect=10, sock_read=timeout
                    ),
                ) as resp:
                    resp.raise_for_status()
                    self._check_audio_type(resp)
                    length = resp.content_length or 0
                    if length and length > limit:
                        raise ValueError(
                            f"文件 {length / 1024 / 1024:.1f}MB 超过上限 {max_mb}MB"
                        )
                    with open(dest, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            written += len(chunk)
                            if written > limit:
                                raise ValueError(f"文件超过下载上限 {max_mb}MB")
                            f.write(chunk)
                if not written:
                    raise ValueError("音频响应为空")
                return written
            except Exception as e:
                last_err = e
                if attempt == 0:
                    logger.warning(f"[ncm_player] 下载失败，3 秒后重试: {e}")
                    await asyncio.sleep(3)
        raise last_err
