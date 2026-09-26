"""网易云音乐 API 封装。

音源四级链路：
1. 自建/公共 NeteaseCloudMusicApi 服务（支持二维码登录，音质可控到母带级）
2. 网易云官方网页接口（无登录态时多数返回 -110，仅作尝试）
3. Meting 镜像（音质不可控，通常 128-320k mp3）
4. 网易云官方外链兜底

登录态感知：
- 接口返回带 freeTrialInfo 的地址 = 30 秒试听片段（登录失效/未登录/非会员），
  绝不当作成功，立即复核登录态并自动降级到 Meting 镜像；
- probe_login() 解析 /login/status 的 account 与 vipType（缺失时以 /user/detail
  二次确认），维护 login_valid/vip，供插件在点歌时给出「登录失效」提示。
"""

import asyncio
import re
import time
from dataclasses import dataclass

import aiohttp

from astrbot.api import logger

# 音质档位 -> (显示名, 官方接口码率, ncm_api level)
QUALITY_LEVELS: dict[str, tuple[str, int, str]] = {
    "standard": ("标准 128k", 128000, "standard"),
    "higher": ("较高 192k", 192000, "higher"),
    "exhigh": ("极高 320k", 320000, "exhigh"),
    "lossless": ("无损 FLAC", 999000, "lossless"),
    "hires": ("高清臻音 Hi-Res", 999000, "hires"),
    "jymaster": ("超清母带", 999000, "jymaster"),
}
# 取不到期望档位时的回退顺序
LEVEL_FALLBACK = ["exhigh", "higher", "standard"]

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
    br: int = 0        # 实际码率，0 表示未知
    size: int = 0      # 字节，0 表示未知
    ext: str = "mp3"
    level: str = ""    # 音质档位 key，空表示未知来源
    source: str = ""   # 来源：ncm / web / meting / outer
    fee: int = 0       # 1=VIP 歌曲 4=付费专辑，0/8=免费（用于卡片 VIP 标记）

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
        if self.br >= 999000:
            return "无损"
        if self.br > 0:
            return f"{self.br // 1000}kbps"
        return "在线"


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
        "Cookie": "appver=2.0.2",
    }

    def __init__(
        self,
        proxy: str = "",
        ncm_api_base: str = "",
        meting_api: str = "",
        cookie: str = "",
    ):
        self.proxy = proxy or None
        self.ncm_api_base = ncm_api_base.rstrip("/") if ncm_api_base else ""
        self.meting_api = meting_api if meting_api else ""
        self.cookie = cookie or ""
        # 登录态：True/False 已确认，None 未知；点歌提示据此判断
        self.login_valid: bool | None = None
        self.vip: bool = False
        self._login_check_ts: float = 0.0
        # 上一次取播放地址是否因「试听片段」降级（"" / "trial"）
        self.last_fallback: str = ""
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

    def set_cookie(self, cookie: str):
        self.cookie = cookie or ""

    def _auth_headers(self) -> dict:
        h = dict(self.HEADERS)
        h["Cookie"] = (
            f"appver=2.0.2; {self.cookie}" if self.cookie else "appver=2.0.2"
        )
        return h

    async def _get(self, url: str, auth: bool = False, **kwargs):
        headers = self._auth_headers() if auth else None
        async with self.session.get(
            url, proxy=self.proxy, headers=headers, **kwargs
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
        """响应带 freeTrialInfo = 返回的是 30 秒试听片段（登录失效/未登录/非会员）。
        此类 URL 绝不当作成功，否则用户收到的永远是试听部分。"""
        return bool(d.get("freeTrialInfo"))

    async def get_play_info(self, song_id: int, quality: str) -> PlayInfo | None:
        """按音质档位获取播放地址，逐级回退；遇到试听片段自动复核登录态并降级 Meting"""
        prefer = QUALITY_LEVELS.get(quality, QUALITY_LEVELS["exhigh"])
        self.last_fallback = ""

        # 1. NeteaseCloudMusicApi 服务（支持登录 cookie，可到母带级）
        if self.ncm_api_base:
            levels = list(dict.fromkeys([prefer[2], *LEVEL_FALLBACK]))
            trial = False
            trial_fee = 0
            for lv in levels:
                try:
                    # timestamp 破缓存：服务端 apicache 按 URL 缓存、不区分 Cookie，
                    # 否则匿名期请求过的歌会长期命中 30 秒试听的缓存结果
                    result = await self._get(
                        f"{self.ncm_api_base}/song/url/v1",
                        auth=True,
                        params={"id": song_id, "level": lv, "timestamp": self._ts()},
                    )
                    d = (result.get("data") or [{}])[0]
                    if not d.get("url"):
                        continue
                    if self._is_trial(d):
                        # 试听片段：换档位也是试听，立即复核登录态并跳出，降级镜像
                        logger.warning(
                            f"[ncm_player] 歌曲 {song_id} 仅返回试听片段"
                            f"（freeTrialInfo），登录态失效或账号非会员，自动降级镜像"
                        )
                        trial = True
                        trial_fee = int(d.get("fee", 0) or 0)
                        await self.probe_login()
                        break
                    return PlayInfo(
                        url=d["url"],
                        br=d.get("br", 0),
                        size=d.get("size", 0),
                        ext=(d.get("type") or "mp3").lower(),
                        level=lv,
                        source="ncm",
                        fee=int(d.get("fee", 0) or 0),
                    )
                except Exception as e:
                    logger.warning(f"[ncm_player] ncm_api 取地址失败(level={lv}): {e}")
            if trial:
                # 官方网页接口无登录态同样只会给试听，直接进入 Meting 镜像
                return self._meting_or_outer(song_id, reason="trial", fee=trial_fee)

        # 2. 官方网页接口，按码率回退
        brs = sorted(
            {prefer[1], *[QUALITY_LEVELS[k][1] for k in LEVEL_FALLBACK]},
            reverse=True,
        )
        for br in brs:
            try:
                result = await self._get(
                    self.PLAY_URL, params={"ids": f"[{song_id}]", "br": br}
                )
                data = (result.get("data") or [{}])[0]
                if not data.get("url"):
                    continue
                if self._is_trial(data):
                    logger.warning(
                        f"[ncm_player] 歌曲 {song_id} 官方接口仅返回试听片段，自动降级镜像"
                    )
                    await self.probe_login()
                    return self._meting_or_outer(
                        song_id, reason="trial", fee=int(data.get("fee", 0) or 0)
                    )
                return PlayInfo(
                    url=data["url"],
                    br=data.get("br", br),
                    size=data.get("size", 0),
                    ext=(data.get("type") or "mp3").lower(),
                    source="web",
                    fee=int(data.get("fee", 0) or 0),
                )
            except Exception as e:
                logger.warning(f"[ncm_player] 官方接口取地址失败(br={br}): {e}")

        # 3/4. Meting 镜像 → 官方外链兜底
        return self._meting_or_outer(song_id)

    def _meting_or_outer(self, song_id: int, reason: str = "", fee: int = 0) -> PlayInfo:
        """Meting 镜像 → 官方外链。reason='trial' 表示因试听片段降级；fee 透传 VIP 标记"""
        if reason == "trial":
            self.last_fallback = "trial"
        if self.meting_api:
            sep = "&" if "?" in self.meting_api else "?"
            return PlayInfo(
                url=f"{self.meting_api}{sep}server=netease&type=url&id={song_id}",
                br=0,
                size=0,
                ext="mp3",
                source="meting",
                fee=fee,
            )
        return PlayInfo(
            url=f"{self.OUTER_URL}?id={song_id}.mp3", br=0, size=0, ext="mp3",
            source="outer", fee=fee,
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

    async def qr_key(self) -> str:
        result = await self._get(
            f"{self.ncm_api_base}/login/qr/key",
            params={"timestamp": self._ts()},
        )
        return (result.get("data") or {})["unikey"]

    async def qr_create(self, key: str) -> str:
        """返回二维码图片的 base64 data-uri"""
        result = await self._get(
            f"{self.ncm_api_base}/login/qr/create",
            params={"key": key, "qrimg": "true", "timestamp": self._ts()},
        )
        return (result.get("data") or {})["qrimg"]

    async def qr_check(self, key: str) -> tuple[int, str]:
        """轮询扫码状态。返回 (code, cookie)。800 过期 801 等待 802 待确认 803 成功。

        cookie 优先取响应体；部分版本只在 Set-Cookie 响应头里返回，做兜底合并，
        避免登录成功却拿到空 cookie（表现为重启/刷新后"掉登录"）。
        """
        async with self.session.get(
            f"{self.ncm_api_base}/login/qr/check",
            proxy=self.proxy,
            headers=self._auth_headers(),
            params={"key": key, "timestamp": self._ts()},
        ) as resp:
            resp.raise_for_status()
            result = await resp.json(content_type=None)
            cookie = result.get("cookie", "") or ""
            if not cookie:
                raw = resp.headers.getall("Set-Cookie", [])
                pairs = [c.split(";", 1)[0] for c in raw if "=" in c.split(";", 1)[0]]
                cookie = "; ".join(pairs)
            return int(result.get("code", 0)), cookie

    # 登录态复核防抖间隔（秒）：避免每首 VIP 歌都重复打 /login/status
    LOGIN_CHECK_INTERVAL = 60

    async def probe_login(self, force: bool = False) -> bool:
        """复核登录态与会员身份，维护 login_valid / vip。返回当前登录是否有效。

        - 明确响应（有/无 account）才会翻转状态；网络异常保留既有状态，
          避免一次抖动把「已登录」误判成「失效」导致误报提示。
        - 60 秒防抖（force=True 强制复核，用于扫码登录成功后立即确认）。
        """
        if not self.ncm_api_base or not self.cookie:
            self.login_valid = False
            self.vip = False
            return False
        if not force and time.time() - self._login_check_ts < self.LOGIN_CHECK_INTERVAL:
            return bool(self.login_valid)
        self._login_check_ts = time.time()
        try:
            result = await self._get(
                f"{self.ncm_api_base}/login/status",
                auth=True,
                params={"timestamp": self._ts()},
            )
            # 兼容两种响应结构：{data:{account,profile}} 或顶层 {account,profile}
            data = result.get("data") or result
            account = data.get("account") or {}
            profile = data.get("profile") or {}
            self.login_valid = bool(account.get("id"))
            # /login/status 走 /api/w/nuser/account/get，是轻量接口，
            # profile 经常不含 vipType（SVIP 也会被误判为非会员）；
            # 拿不到 vipType 时用 /user/detail 的完整 profile 二次确认
            vip_type = profile.get("vipType") or 0
            if self.login_valid and not vip_type:
                try:
                    detail = await self._get(
                        f"{self.ncm_api_base}/user/detail",
                        auth=True,
                        params={"uid": account["id"], "timestamp": self._ts()},
                    )
                    vip_type = (detail.get("profile") or {}).get("vipType") or 0
                except Exception as e:
                    logger.warning(f"[ncm_player] 获取用户详情失败（按非 VIP 处理）: {e}")
            self.vip = bool(vip_type)
            if not self.login_valid:
                logger.warning(
                    "[ncm_player] 登录复核：cookie 已失效（接口未返回账号信息）"
                )
        except Exception as e:
            logger.warning(f"[ncm_player] 登录状态校验失败（保留原状态）: {e}")
        return bool(self.login_valid)

    async def check_login(self) -> bool:
        """校验当前 cookie 是否仍有效（用于启动时提示登录状态）"""
        return await self.probe_login(force=True)

    # ---------- 下载 ----------

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
                    proxy=self.proxy,
                    timeout=aiohttp.ClientTimeout(
                        total=None, connect=10, sock_connect=10, sock_read=timeout
                    ),
                ) as resp:
                    resp.raise_for_status()
                    ctype = (resp.content_type or "").lower()
                    if ctype and not (
                        ctype.startswith("audio")
                        or ctype == "application/octet-stream"
                    ):
                        raise ValueError(
                            f"返回的不是音频(content-type={ctype})，歌曲可能不可用"
                        )
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
                return written
            except Exception as e:
                last_err = e
                if attempt == 0:
                    logger.warning(f"[ncm_player] 下载失败，3 秒后重试: {e}")
                    await asyncio.sleep(3)
        raise last_err
