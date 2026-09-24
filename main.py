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

# 关键词监听触发词
LISTEN_TRIGGERS = ["我要听", "我想听", "想听", "听歌", "点歌", "来一首", "来首", "放一首", "放首", "播放"]
LISTEN_PATTERN = "(" + "|".join(LISTEN_TRIGGERS) + ")"


@register(
    "astrbot_plugin_ncm_player",
    "Kimi",
    "网易云点歌：关键词监听/自然语言点歌、CD 风选歌图、语音/文件/卡片发送、热评卡片、歌词合并转发、扫码登录、内置 NeteaseCloudMusicApi 服务",
    "1.4.2",
)
class NcmPlayerPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.cfg = config or {}
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_ncm_player")
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = self.data_dir / "ncm_cookie.txt"
        cookie = (
            self.cookie_file.read_text(encoding="utf-8").strip()
            if self.cookie_file.exists()
            else ""
        )
        self.api = NetEaseAPI(
            proxy=self.cfg.get("http_proxy", ""),
            ncm_api_base=self.cfg.get("ncm_api_base", ""),
            meting_api=self.cfg.get("meting_api", "https://api.qijieya.cn/meting/"),
            cookie=cookie,
        )
        self.renderer = CardRenderer(self.cache_dir)
        self.sender = SongSender(self.cfg)
        # unified_msg_origin -> (时间戳, 候选歌曲)
        self.pending: dict[str, tuple[float, list[Song]]] = {}
        # 内置 NeteaseCloudMusicApi 服务（默认关闭：不下载、不启动进程）
        self.embedded: EmbeddedNcmServer | None = None
        if self.cfg.get("ncm_api_embedded", False):
            self.embedded = EmbeddedNcmServer(
                self.data_dir,
                port=int(self.cfg.get("ncm_api_embedded_port", 13000)),
                proxy=self.cfg.get("http_proxy", ""),
                mirror=self.cfg.get("ncm_api_embedded_mirror", ""),
            )

    async def initialize(self):
        """插件加载后钩子。

        内置服务启动与登录状态校验全部放入后台任务，绝不阻塞 AstrBot 启动流程
        （同步等待二进制下载/健康检查曾导致插件重载卡住、AstrBot 无法重启）。
        """
        self._bg_task = asyncio.create_task(self._init_background())

    async def _init_background(self):
        """后台初始化：按需启动内置服务并校验登录状态"""
        if self.embedded:
            try:
                base = await self.embedded.start()
                # 内置服务就绪后优先生效（本机服务，音质可控、支持扫码登录）
                self.api.ncm_api_base = base
                logger.info(f"[ncm_player] 已切换到内置 NeteaseCloudMusicApi: {base}")
            except Exception as e:
                logger.error(
                    f"[ncm_player] 内置 NeteaseCloudMusicApi 启动失败，"
                    f"回退到既有音源链路: {e}"
                )
        if self.api.ncm_api_base and self.api.cookie:
            if await self.api.check_login():
                logger.info("[ncm_player] 检测到已登录的网易云账号，会员音质可用")
            else:
                logger.warning(
                    "[ncm_player] 本地 cookie 已失效或未登录，"
                    "无损及以上音质不可用，可使用 /网易云登录 重新扫码"
                )

    async def terminate(self):
        task = getattr(self, "_bg_task", None)
        if task and not task.done():
            task.cancel()
        if self.embedded:
            await self.embedded.stop()
        await self.api.close()

    # ---------- 内部流程 ----------

    def _get_pending(self, event: AstrMessageEvent) -> list[Song] | None:
        item = self.pending.get(event.unified_msg_origin)
        if item and time.time() - item[0] < PENDING_EXPIRE:
            return item[1]
        return None

    async def _search_and_render(self, event: AstrMessageEvent, keyword: str) -> list[Song] | None:
        """搜索并发送选歌图，返回候选列表（已缓存）"""
        limit = int(self.cfg.get("search_limit", 5))
        try:
            songs = await self.api.search(keyword, limit=limit)
        except Exception as e:
            logger.error(f"[ncm_player] 搜索失败: {e}")
            await event.send(event.plain_result(f"搜索失败：{e}"))
            return None
        if not songs:
            await event.send(event.plain_result(f"没有找到「{keyword}」相关的歌曲"))
            return None

        covers = await asyncio.gather(*[self.api.fetch_bytes(s.pic_url) for s in songs])
        try:
            img_path = self.renderer.render_selection(songs, list(covers), keyword)
            await event.send(event.chain_result([Image.fromFileSystem(img_path)]))
        except Exception as e:
            logger.error(f"[ncm_player] 选歌图渲染失败，改用文本列表: {e}")
            text = "\n".join(
                f"{i + 1}. {s.name} - {s.artists}" for i, s in enumerate(songs)
            )
            await event.send(event.plain_result(f"🎵 搜索结果：\n{text}\n回复序号点歌"))

        self.pending[event.unified_msg_origin] = (time.time(), songs)
        return songs

    async def _download(self, song: Song, play: PlayInfo) -> str | None:
        """按配置下载音频，超限/超时/失败返回 None"""
        max_mb = int(self.cfg.get("download_max_mb", 40))
        if play.size and play.size > max_mb * 1024 * 1024:
            logger.warning(
                f"[ncm_player] 《{song.name}》{play.size_mb:.1f}MB 超过上限 {max_mb}MB，跳过下载"
            )
            return None
        dest = self.cache_dir / f"{song.id}_{uuid.uuid4().hex[:8]}.{play.ext}"
        try:
            await self.api.download(
                play.url,
                str(dest),
                max_mb=max_mb,
                timeout=int(self.cfg.get("download_timeout", 20)),
            )
            return str(dest)
        except Exception as e:
            logger.warning(f"[ncm_player] 下载失败: {e}")
            dest.unlink(missing_ok=True)
            return None

    async def _send_comment_card(self, event: AstrMessageEvent, song: Song):
        """嗅探一条热评，渲染成卡片发送"""
        try:
            comment = await self.api.fetch_comment(song.id)
            if not comment:
                return
            avatar = await self.api.fetch_bytes(comment.avatar_url)
            path = self.renderer.render_comment(comment, song, avatar)
            await event.send(event.chain_result([Image.fromFileSystem(path)]))
        except Exception as e:
            logger.warning(f"[ncm_player] 热评卡片发送失败: {e}")

    async def _send_lyrics_forward(self, event: AstrMessageEvent, song: Song):
        """嗅探整首歌词，以合并转发（聊天记录）形式发送"""
        try:
            lines = await self.api.fetch_lyric(song.id)
            if not lines:
                return
            try:
                uin = str(event.get_self_id())
            except Exception:
                uin = "0"
            title = f"《{song.name}》- {song.artists} 歌词"
            nodes = [
                Node(uin=uin, name="网易云歌词", content=[Plain(f"🎵 {title}\n共 {len(lines)} 行")])
            ]
            for i in range(0, len(lines), LYRICS_PER_NODE):
                chunk = lines[i : i + LYRICS_PER_NODE]
                nodes.append(
                    Node(uin=uin, name="网易云歌词", content=[Plain("\n".join(chunk))])
                )
            await event.send(event.chain_result([Nodes(nodes)]))
        except Exception as e:
            logger.warning(f"[ncm_player] 歌词合并转发发送失败: {e}")

    async def _play(self, event: AstrMessageEvent, song: Song) -> str:
        """取播放地址 → 下载（如需）→ 播放卡片 → 发送 → 热评/歌词。返回结果描述"""
        quality = normalize_quality(self.cfg.get("quality", "exhigh"))
        play = await self.api.get_play_info(song.id, quality)
        if not play or not play.url:
            return f"未能获取《{song.name}》的播放地址（可能为 VIP/无版权歌曲）"

        load_mode = str(self.cfg.get("load_mode", "file"))
        audio_path = None
        if load_mode in ("file", "base64"):
            audio_path = await self._download(song, play)

        # 播放卡片图（CD + 歌名）
        if self.cfg.get("send_play_card", True):
            try:
                cover = await self.api.fetch_bytes(song.pic_url)
                card = self.renderer.render_playing(song, cover, play.quality_str)
                await event.send(event.chain_result([Image.fromFileSystem(card)]))
            except Exception as e:
                logger.warning(f"[ncm_player] 播放卡片发送失败: {e}")

        method = await self.sender.send(event, song, play, audio_path)

        # 热评卡片 + 歌词合并转发（独立于歌曲发送，失败互不影响）
        if self.cfg.get("send_comment_card", True):
            await self._send_comment_card(event, song)
        if self.cfg.get("send_lyrics_forward", True):
            await self._send_lyrics_forward(event, song)

        # 清理本次下载的缓存文件（延迟删除，等协议端读取完毕）
        if audio_path:
            try:
                from pathlib import Path as _P

                asyncio.get_running_loop().call_later(
                    120, lambda p=audio_path: _P(p).unlink(missing_ok=True)
                )
            except Exception:
                pass
        return f"《{song.name}》- {song.artists}，音质 {play.quality_str}，以「{method}」方式发送"

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="search_songs")
    async def search_songs(self, event: AstrMessageEvent, keyword: str):
        """【网易云点歌·搜索选歌】点歌场景第一步：搜索歌曲并以图片形式把候选列表（含封面、歌名、歌手）发给用户挑选。
        触发场景：用户说"我想听xx"、"我要听xx"、"点歌"、"来一首xx"、"播放xx"等，但没有明确指定唯一一首歌（只说歌手/风格/模糊描述，或明确说"让我选"）时，必须调用本工具。
        若用户已给出明确具体的歌名，应直接调用 play_song，不必先搜索。

        Args:
            keyword(string): 搜索关键词，建议带上歌手提高准确度，如"晴天 周杰伦"
        """
        songs = await self._search_and_render(event, keyword)
        if not songs:
            return "搜索失败或没有结果，请换个关键词"
        names = "、".join(f"{i + 1}.{s.name}" for i, s in enumerate(songs))
        return (
            f"已向用户发送选歌列表图片：{names}。"
            "请引导用户回复序号（1-%d），用户回复后调用 play_song(index=序号) 播放。"
            % len(songs)
        )

    @filter.llm_tool(name="play_song")
    async def play_song(
        self, event: AstrMessageEvent, song_name: str = "", index: int = 0
    ):
        """【网易云点歌·播放】播放歌曲，自动完成搜索、下载，并以语音/文件/音乐卡片发送，附带封面卡片、热评和歌词。
        触发场景一：用户明确说想听某首歌（如"我要听晴天"、"放一首起风了"），将歌名传入 song_name 直接播放，必须调用本工具而不是仅文字回复。
        触发场景二：此前调用过 search_songs 且用户回复了序号（如"1"、"第2个"），传入 index（从 1 开始）播放对应歌曲。

        Args:
            song_name(string): 歌曲名称，可带歌手提高准确度，如"晴天 周杰伦"。index 大于 0 时可为空
            index(number): 选歌列表中的序号，从 1 开始；用户未回复序号时填 0
        """
        try:
            index = int(index or 0)
        except (TypeError, ValueError):
            index = 0
        song: Song | None = None
        if index >= 1:
            songs = self._get_pending(event)
            if songs and 1 <= index <= len(songs):
                song = songs[index - 1]
            elif not song_name:
                return "选歌列表已过期或序号无效，请重新搜索"
        if song is None:
            if not song_name:
                return "请提供歌名或有效的选歌序号"
            try:
                songs = await self.api.search(song_name, limit=1)
            except Exception as e:
                return f"搜索失败：{e}"
            if not songs:
                return f"没有找到「{song_name}」相关的歌曲"
            song = songs[0]

        result = await self._play(event, song)
        return f"已完成点歌：{result}"

    # ---------- 关键词监听（不依赖 LLM） ----------

    @filter.regex(LISTEN_PATTERN)
    async def listen_keyword(self, event: AstrMessageEvent):
        """监听「我想听/点歌/来一首」等口语化点歌请求，直接搜歌发选歌图"""
        if not self.cfg.get("enable_keyword_listen", True):
            return
        raw = (event.message_str or "").strip()
        if not raw or raw.startswith("/"):
            return  # 指令交给对应 command 处理
        # 去掉 @ 提及组件
        text = re.sub(r"\[At[^\]]*\]", "", raw).strip()
        m = re.search(LISTEN_PATTERN, text)
        if not m or m.start() > 6:
            return  # 触发词位置太靠后，多半是闲聊而非点歌
        rest = text[m.end():]
        if rest[:1] == m.group(1)[-1:]:
            return  # 「想听听…」叠词，非点歌
        book = re.search(r"《(.+?)》", rest)
        keyword = (book.group(1) if book else rest).strip(" ，。~～！!？?、:：的呢吧啊")
        if not keyword or len(keyword) > 30:
            return  # 没有明确歌名，交给 LLM 对话处理
        if keyword.startswith(("什么", "啥", "哪", "怎")):
            return  # 疑问句（"想听什么"），非点歌
        event.stop_event()
        songs = await self._search_and_render(event, keyword)
        if songs:
            await event.send(
                event.plain_result(f"回复序号（1-{len(songs)}）播放，5 分钟内有效喵")
            )

    @filter.regex(r"^\s*(\d{1,2})\s*$")
    async def pick_by_number(self, event: AstrMessageEvent):
        """选歌列表待选期间，纯数字消息视为点歌序号"""
        songs = self._get_pending(event)
        if not songs:
            return
        idx = int(event.message_str.strip())
        if not (1 <= idx <= len(songs)):
            return
        event.stop_event()
        self.pending.pop(event.unified_msg_origin, None)
        result = await self._play(event, songs[idx - 1])
        logger.info(f"[ncm_player] 点歌完成：{result}")

    # ---------- 指令 ----------

    @filter.command("点歌")
    async def cmd_diange(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索歌曲并发送选歌图，回复序号播放。用法：/点歌 歌名"""
        keyword = keyword.strip()
        if not keyword:
            yield event.plain_result("用法：/点歌 歌名")
            return

        songs = await self._search_and_render(event, keyword)
        if not songs:
            return

        @session_waiter(timeout=120)
        async def pick_waiter(controller: SessionController, ev: AstrMessageEvent):
            text = ev.message_str.strip()
            if text in ("取消", "算了", "不用了"):
                await ev.send(ev.plain_result("已取消点歌"))
                controller.stop()
                return
            if not text.isdigit() or not (1 <= int(text) <= len(songs)):
                await ev.send(
                    ev.plain_result(f"请回复 1-{len(songs)} 的序号，或发送「取消」")
                )
                return
            result = await self._play(ev, songs[int(text) - 1])
            logger.info(f"[ncm_player] 点歌完成：{result}")
            controller.stop()

        try:
            await pick_waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("点歌等待超时，已取消"))
        except Exception as e:
            logger.error(f"[ncm_player] 选歌会话异常: {e}")

    @filter.command("直接点歌")
    async def cmd_play_first(self, event: AstrMessageEvent, keyword: str = ""):
        """不经过选歌，直接播放搜索到的第一首。用法：/直接点歌 歌名"""
        keyword = keyword.strip()
        if not keyword:
            yield event.plain_result("用法：/直接点歌 歌名")
            return
        try:
            songs = await self.api.search(keyword, limit=1)
        except Exception as e:
            yield event.plain_result(f"搜索失败：{e}")
            return
        if not songs:
            yield event.plain_result(f"没有找到「{keyword}」相关的歌曲")
            return
        result = await self._play(event, songs[0])
        logger.info(f"[ncm_player] 点歌完成：{result}")
        return

    @filter.command("网易云登录")
    async def cmd_login(self, event: AstrMessageEvent):
        """生成网易云登录二维码，用网易云音乐 App 扫码登录，解锁无损/母带音质"""
        if not self.api.ncm_api_base:
            yield event.plain_result(
                "请先在插件配置中开启「内置 NeteaseCloudMusicApi 服务」"
                "或填写 ncm_api_base（外部服务地址），再使用本命令登录"
            )
            return
        try:
            key = await self.api.qr_key()
            qrimg = await self.api.qr_create(key)
        except Exception as e:
            yield event.plain_result(f"生成二维码失败：{e}")
            return

        # qrimg 是 data:image/png;base64,... 形式
        try:
            b64 = qrimg.split(",", 1)[1]
            qr_path = self.cache_dir / "login_qr.png"
            qr_path.write_bytes(base64.b64decode(b64))
            await event.send(
                event.chain_result(
                    [
                        Image.fromFileSystem(str(qr_path)),
                        Plain("请用网易云音乐 App 扫码登录（3 分钟内有效）"),
                    ]
                )
            )
        except Exception as e:
            yield event.plain_result(f"二维码解析失败：{e}")
            return

        confirmed = False
        for _ in range(120):  # 每 1.5 秒轮询一次，共 3 分钟
            await asyncio.sleep(1.5)
            try:
                code, cookie = await self.api.qr_check(key)
            except Exception as e:
                logger.warning(f"[ncm_player] 扫码状态查询失败: {e}")
                continue
            if code == 803:
                if cookie:
                    self.api.set_cookie(cookie)
                    self.cookie_file.write_text(cookie, encoding="utf-8")
                await event.send(
                    event.plain_result("✅ 网易云登录成功，已解锁会员音质")
                )
                return
            if code == 802 and not confirmed:
                confirmed = True
                await event.send(
                    event.plain_result("扫码成功，请在网易云音乐 App 上点击确认登录")
                )
            if code == 800:
                await event.send(event.plain_result("二维码已过期，请重新发起登录"))
                return
        await event.send(event.plain_result("登录等待超时，请重新发起"))
