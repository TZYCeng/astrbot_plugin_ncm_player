import asyncio
import base64
import contextlib
import os
import re
import time
import uuid
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .core.auth import cookie_keys, has_music_u, normalize_cookie
from .core.ncm_api import (
    NetEaseAPI,
    PlaybackTrace,
    PlayInfo,
    Song,
    normalize_quality,
)
from .core.ncm_server import RELEASE_TAG, EmbeddedNcmServer
from .core.renderer import CardRenderer
from .core.sender import SongSender

PENDING_EXPIRE = 300  # 选歌缓存有效期（秒）
LYRICS_PER_NODE = 30  # 合并转发每条消息的歌词行数
INVALID_NOTICE_INTERVAL = 600  # 登录失效提示最小间隔（秒），避免刷屏
CAPTCHA_COOLDOWN = 60  # 每个管理员发送短信的最小间隔（秒）

# 关键词监听触发词
LISTEN_TRIGGERS = [
    "我要听",
    "我想听",
    "想听",
    "听歌",
    "点歌",
    "来一首",
    "来首",
    "放一首",
    "放首",
    "播放",
]
LISTEN_PATTERN = "(" + "|".join(LISTEN_TRIGGERS) + ")"


def _mask_name(s: str) -> str:
    """账号名打码：保留首尾，中间用 * 代替（userName 可能是手机号）"""
    s = s.strip()
    if len(s) <= 2:
        return s
    return s[:2] + "***" + s[-2:]


@register(
    "astrbot_plugin_ncm_player",
    "Kimi",
    "网易云点歌：关键词监听/自然语言点歌、CD 风选歌图、语音/文件/卡片发送、热评卡片、歌词合并转发、扫码/短信验证码登录、内置 NeteaseCloudMusicApi 服务",
    "1.5.0",
)
class NcmPlayerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_ncm_player")
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = self.data_dir / "ncm_cookie.txt"
        try:
            cookie = (
                self.cookie_file.read_text(encoding="utf-8")
                if self.cookie_file.exists()
                else ""
            )
        except OSError:
            cookie = ""
            logger.warning("[ncm_player] 无法读取已保存的 Cookie，请检查数据目录权限")
        self.api = NetEaseAPI(
            proxy=self.cfg.get("http_proxy", ""),
            ncm_api_base=self._external_base(),
            meting_api=self.cfg.get("meting_api", "https://api.qijieya.cn/meting/"),
            cookie=cookie,
            web_cookie=self.cfg.get("ncm_web_cookie", ""),
            web_cookie_enabled=self.cfg.get("ncm_web_cookie_enabled", False),
            web_enabled=self.cfg.get("ncm_web_enabled", True),
            meting_enabled=self.cfg.get("meting_enabled", True),
            outer_enabled=self.cfg.get("ncm_outer_enabled", True),
        )
        self.renderer = CardRenderer(self.cache_dir)
        self.sender = SongSender(self.cfg)
        # unified_msg_origin -> (时间戳, 候选歌曲)
        self.pending: dict[str, tuple[float, list[Song]]] = {}
        self._waiting: dict[str, list[Song]] = {}
        self._invalid_notice_ts = 0.0
        self._login_lock = asyncio.Lock()
        self._login_generation = 0
        self._login_active = False
        self._captcha_controller: SessionController | None = None
        self._captcha_sent_at: dict[str, float] = {}
        self._active_qr_path = None
        self._service_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._last_playback = "尚未点歌"
        self._active_downloads: set = set()
        # 内置 NeteaseCloudMusicApi 服务（默认关闭：不下载、不启动进程）
        self.embedded = EmbeddedNcmServer(
            self.data_dir,
            port=int(self.cfg.get("ncm_api_embedded_port", 13000)),
            proxy=self.cfg.get("http_proxy", ""),
            mirror=self.cfg.get("ncm_api_embedded_mirror", ""),
            acceleration_enabled=self.cfg.get("ncm_api_github_acceleration", True),
            accelerators=self.cfg.get("ncm_api_github_accelerators"),
            log_output=self.cfg.get("ncm_api_log_output", False),
        )

    def _external_base(self) -> str:
        return (
            str(self.cfg.get("ncm_api_base", "")).strip()
            if self.cfg.get("ncm_api_external_enabled", True)
            else ""
        )

    def _update_sources(self, embedded_url: str = ""):
        self.api.set_api_sources(
            [
                ("内置 API", embedded_url),
                ("外部 API", self._external_base()),
            ]
        )

    async def initialize(self):
        """插件加载后钩子。

        内置服务启动与登录状态校验全部放入后台任务，绝不阻塞 AstrBot 启动流程
        （同步等待二进制下载/健康检查曾导致插件重载卡住、AstrBot 无法重启）。
        """
        self._bg_task = asyncio.create_task(self._init_background())

    async def _init_background(self):
        """后台初始化：按需启动内置服务并校验登录状态"""
        async with self._service_lock:
            if self.cfg.get("ncm_api_embedded", False):
                try:
                    self._update_sources(await self.embedded.start())
                except Exception as exc:
                    logger.error(
                        f"[ncm_player] 内置 API 启动失败，继续使用其他已启用音源: {exc}"
                    )
        await self._probe_accounts()

    async def _probe_accounts(self):
        checks = [self.api.probe_login(force=True)]
        if self.api.web_enabled and self.api.web_cookie_enabled:
            checks.append(self.api.probe_login(force=True, credential="web"))
        await asyncio.gather(*checks)

    async def _cancel_background(self):
        task = getattr(self, "_bg_task", None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def terminate(self):
        await self._cancel_background()
        async with self._service_lock:
            await self.embedded.stop()
        await self.api.close()

    # ---------- 内部流程 ----------

    @staticmethod
    def _pend_key(event: AstrMessageEvent) -> str:
        """选歌状态键：同一群聊中不同用户各自独立，避免 A 选歌时 B 的回复串台"""
        try:
            sender = str(event.get_sender_id())
        except Exception:
            sender = ""
        return f"{event.unified_msg_origin}#{sender}"

    def _get_pending(self, event: AstrMessageEvent) -> list[Song] | None:
        item = self.pending.get(self._pend_key(event))
        if item and time.time() - item[0] < PENDING_EXPIRE:
            return item[1]
        if item:
            self.pending.pop(self._pend_key(event), None)
        return None

    def _clear_pending(self, origin: str, songs: list[Song]):
        item = self.pending.get(origin)
        if item and item[1] is songs:
            self.pending.pop(origin, None)

    async def _search_and_render(
        self, event: AstrMessageEvent, keyword: str
    ) -> list[Song] | None:
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

        origin = self._pend_key(event)
        self.pending[origin] = (time.time(), songs)
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
        self._active_downloads.add(dest)
        completed = False
        try:
            await self.api.download(
                play.url,
                str(dest),
                max_mb=max_mb,
                timeout=int(self.cfg.get("download_timeout", 20)),
            )
            completed = True
            return str(dest)
        except Exception as e:
            logger.warning(f"[ncm_player] 下载失败: {e}")
            return None
        finally:
            # 取消也必须移除半成品；成功文件在发送/协议端读取期间继续受保护。
            if not completed:
                self._expire_audio(dest)

    def _expire_audio(self, path: Path):
        self._active_downloads.discard(path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("[ncm_player] 音频缓存删除失败，可稍后使用清理缓存命令")

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
                Node(
                    uin=uin,
                    name="网易云歌词",
                    content=[Plain(f"🎵 {title}\n共 {len(lines)} 行")],
                )
            ]
            for i in range(0, len(lines), LYRICS_PER_NODE):
                chunk = lines[i : i + LYRICS_PER_NODE]
                nodes.append(
                    Node(uin=uin, name="网易云歌词", content=[Plain("\n".join(chunk))])
                )
            await event.send(event.chain_result([Nodes(nodes)]))
        except Exception as e:
            logger.warning(f"[ncm_player] 歌词合并转发发送失败: {e}")

    async def _notify_login_invalid(self, event: AstrMessageEvent):
        """登录失效/未登录时给点歌用户一个明确提示（10 分钟内最多一次，避免刷屏）"""
        if not self.api.ncm_api_base:
            return
        if (
            self.api.account.login_valid is not False
            or self.api.web_account.login_valid is True
        ):
            return
        now = time.time()
        if now - self._invalid_notice_ts < INVALID_NOTICE_INTERVAL:
            return
        self._invalid_notice_ts = now
        try:
            await event.send(
                event.plain_result(
                    "⚠️ 网易云 API 账号未登录或登录已失效，将按配置尝试备用音源。"
                    "管理员可使用 /网易云登录，或配置网页 Cookie；详情见 /网易云诊断。"
                )
            )
        except Exception as e:
            logger.warning(f"[ncm_player] 登录失效提示发送失败: {e}")

    async def _play(self, event: AstrMessageEvent, song: Song) -> str:
        """取播放地址 → 下载（如需）→ 播放卡片 → 发送 → 热评/歌词。返回结果描述"""
        quality = normalize_quality(self.cfg.get("quality", "exhigh"))
        load_mode = str(self.cfg.get("load_mode", "file"))
        trace = PlaybackTrace()
        play = None
        audio_path = None
        async for candidate in self.api.iter_play_info(song.id, quality, trace):
            if load_mode in ("file", "base64"):
                audio_path = await self._download(song, candidate)
                available = audio_path is not None
            else:
                # URL 模式仍探测响应，坏镜像链接不能阻止后面的外链兜底。
                available = await self.api.check_audio_url(candidate.url)
            if available:
                play = candidate
                break
            trace.record(candidate.source_label, "下载或音频探测失败")
        if not play:
            self._last_playback = (
                f"歌曲 {song.id} 失败：{trace.summary or '所有音源均已关闭'}"
            )
            message = f"未能获取《{song.name}》的完整音频，已尝试所有启用的音源。"
            await event.send(event.plain_result(f"{message}\n{song.page_url}"))
            return message
        play.fallback_reason = trace.summary
        try:
            await self._notify_login_invalid(event)

            # 播放卡片图（CD + 歌名；VIP 歌曲按下载来源标红/灰 VIP 框）
            if self.cfg.get("send_play_card", True):
                try:
                    vip_mark = ""
                    if play.is_vip_song or trace.trial:
                        vip_mark = (
                            "ok"
                            if play.source in ("ncm", "web") and play.authenticated
                            else "mirror"
                        )
                    cover = await self.api.fetch_bytes(song.pic_url)
                    card = self.renderer.render_playing(
                        song, cover, play.quality_str, vip_mark
                    )
                    await event.send(event.chain_result([Image.fromFileSystem(card)]))
                except Exception as e:
                    logger.warning(f"[ncm_player] 播放卡片发送失败: {e}")
            method = await self.sender.send(event, song, play, audio_path)
        finally:
            # 发送失败或取消仍安排清理，给协议端保留读取本地文件的时间。
            if audio_path:
                asyncio.get_running_loop().call_later(
                    120, self._expire_audio, Path(audio_path)
                )

        # 热评卡片 + 歌词合并转发（独立于歌曲发送，失败互不影响）
        if self.cfg.get("send_comment_card", True):
            await self._send_comment_card(event, song)
        if self.cfg.get("send_lyrics_forward", True):
            await self._send_lyrics_forward(event, song)

        result = f"《{song.name}》- {song.artists}，音质 {play.quality_str}，以「{method}」方式发送"
        result += f"，来源 {play.source_label}"
        if play.fallback_reason:
            result += f"（回退：{play.fallback_reason}）"
        self._last_playback = f"歌曲 {song.id}：{result}"
        return result

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
                self._clear_pending(self._pend_key(event), songs)
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
        rest = text[m.end() :]
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
        origin = self._pend_key(event)
        if (
            origin in self._waiting
            and self._get_pending(event) is self._waiting[origin]
        ):
            return
        songs = self._get_pending(event)
        if not songs:
            return
        idx = int(event.message_str.strip())
        if not (1 <= idx <= len(songs)):
            return
        event.stop_event()
        self._clear_pending(origin, songs)
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

        origin = self._pend_key(event)
        self._waiting[origin] = songs

        @session_waiter(timeout=120)
        async def pick_waiter(controller: SessionController, ev: AstrMessageEvent):
            if self._waiting.get(origin) is not songs:
                controller.stop()
                return
            try:
                same_user = str(ev.get_sender_id()) == str(event.get_sender_id())
            except Exception:
                same_user = False
            if not same_user:
                return  # 同一群聊中其他人的消息：不处理也不结束等待
            text = ev.message_str.strip()
            if self._get_pending(ev) is not songs and text not in (
                "取消",
                "算了",
                "不用了",
            ):
                controller.stop()
                return
            if text in ("取消", "算了", "不用了"):
                self._clear_pending(origin, songs)
                controller.stop()
                await ev.send(ev.plain_result("已取消点歌"))
                return
            if not text.isdigit() or not (1 <= int(text) <= len(songs)):
                await ev.send(
                    ev.plain_result(f"请回复 1-{len(songs)} 的序号，或发送「取消」")
                )
                return
            self._clear_pending(origin, songs)
            controller.stop()
            result = await self._play(ev, songs[int(text) - 1])
            logger.info(f"[ncm_player] 点歌完成：{result}")

        try:
            await pick_waiter(event)
        except asyncio.TimeoutError:
            await event.send(event.plain_result("点歌等待超时，已取消"))
        except Exception as e:
            logger.error(f"[ncm_player] 选歌会话异常: {e}")
        finally:
            self._clear_pending(origin, songs)
            if self._waiting.get(origin) is songs:
                self._waiting.pop(origin, None)

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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("网易云清理缓存")
    async def cmd_clear_cache(self, event: AstrMessageEvent):
        """仅清理本插件 cache 目录的临时文件和选歌候选。"""
        self.pending.clear()
        self._waiting.clear()
        removed, failed = 0, []
        try:
            for path in self.cache_dir.iterdir():
                if (
                    path == self._active_qr_path
                    or path in self._active_downloads
                    or path.is_symlink()
                    or not path.is_file()
                ):
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError as e:
                    failed.append(f"{path.name}: {e}")
        except OSError as e:
            failed.append(f"cache 目录: {e}")
        text = f"已清理 {removed} 个缓存文件及内存选歌候选；Cookie 和服务二进制未删除。"
        if failed:
            text += " 删除失败：" + "；".join(failed)
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("网易云删除API")
    async def cmd_delete_api(self, event: AstrMessageEvent):
        """持久化关闭内置服务，回收进程后删除插件托管的程序和运行缓存。"""
        yield event.plain_result("正在关闭并删除内置 API…")
        yield event.plain_result(await self._manage_api(reinstall=False))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("网易云重装API")
    async def cmd_reinstall_api(self, event: AstrMessageEvent):
        """重新下载固定版本并重置隔离运行目录，同时持久化开启内置服务。"""
        yield event.plain_result("正在重新部署内置 API，首次下载可能需要几分钟…")
        yield event.plain_result(await self._manage_api(reinstall=True))

    def _persist_embedded(self, enabled: bool):
        # 必须持有原始 AstrBotConfig 对象；普通 dict 无法持久化插件配置。
        save = getattr(self.cfg, "save_config", None)
        if not callable(save):
            raise RuntimeError("当前配置对象不支持持久化，请从 AstrBot 插件配置页操作")
        existed = "ncm_api_embedded" in self.cfg
        previous = self.cfg.get("ncm_api_embedded")
        self.cfg["ncm_api_embedded"] = enabled
        try:
            save()
        except Exception:
            if existed:
                self.cfg["ncm_api_embedded"] = previous
            else:
                self.cfg.pop("ncm_api_embedded", None)
            raise

    async def _manage_api(self, reinstall: bool) -> str:
        if self._maintenance_lock.locked():
            return "已有内置 API 管理操作进行中，请等待结束"
        async with self._maintenance_lock:
            try:
                self._persist_embedded(reinstall)
            except Exception as exc:
                logger.error(f"[ncm_player] 内置服务开关保存失败: {type(exc).__name__}")
                return "无法保存内置服务开关，操作已取消，请检查配置文件权限"
            await self._cancel_background()
            async with self._service_lock:
                async with self._login_lock:
                    self._login_generation += 1
                    if self._captcha_controller:
                        self._captcha_controller.stop()
                self._update_sources()
                try:
                    if reinstall:
                        base = await self.embedded.reinstall()
                        self._update_sources(base)
                    else:
                        await self.embedded.remove()
                except Exception as exc:
                    logger.error(
                        f"[ncm_player] 内置 API 管理失败: {type(exc).__name__}"
                    )
                    return (
                        "内置 API 操作失败；其他已启用音源仍可使用。"
                        "使用 /网易云诊断 查看状态并检查数据目录权限、端口或下载源。"
                    )
            await self._probe_accounts()
            if reinstall:
                return f"内置 API {self.embedded.version} 已重新下载、校验并启动；开关已开启，账号已重新复核。"
            return "内置 API 程序及运行缓存已删除，开关已保存为关闭；下次在配置页开启并重载插件时会重新下载。"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("网易云诊断")
    async def cmd_diagnose(self, event: AstrMessageEvent):
        """只展示账号身份与来源，不回显 Cookie、二维码令牌或手机号。"""
        yield event.plain_result("正在复核账号和 API 状态…")
        await self._probe_accounts()
        lines = [
            "网易云诊断",
            f"内置 API：{'开启' if self.cfg.get('ncm_api_embedded') else '关闭'} / {self.embedded.state}",
            f"内置目标版本：{RELEASE_TAG}",
        ]
        if self.embedded.last_error:
            lines.append(f"最近启动错误：{self.embedded.last_error}")
        for service, base in tuple(self.api.api_sources):
            try:
                payload = await self.api._api_request(
                    "/inner/version", base=base, cookie=""
                )
                version = (payload.get("data") or {}).get("version", "版本未知")
                lines.append(f"{service}：可达 / {version}")
            except Exception as exc:
                lines.append(f"{service}：检查失败({type(exc).__name__})")
        if not self.api.api_sources:
            lines.append("当前没有启用且可用的 API 服务")
        for label, account in (
            ("API 登录账号", self.api.account),
            ("网页 Cookie 账号", self.api.web_account),
        ):
            if label.startswith("网页") and not (
                self.api.web_enabled and self.api.web_cookie_enabled
            ):
                lines.append(f"{label}：已关闭")
                continue
            status = (
                "已登录"
                if account.login_valid
                else ("未通过" if account.login_valid is False else "未知")
            )
            lines.extend(
                [
                    f"{label}：{status} / {account.membership}",
                    f"UID：{account.uid or '无'}；昵称：{account.nickname or '无'}；用户名：{_mask_name(account.user_name) if account.user_name else '无'}",
                    f"核验来源：{account.service or '无'}；结果：{account.error or account.reason or '正常'}",
                ]
            )
        lines.append(
            f"音源开关：官方网页={'开' if self.api.web_enabled else '关'}，镜像={'开' if self.api.meting_enabled else '关'}，官方外链={'开' if self.api.outer_enabled else '关'}"
        )
        lines.append(f"最近点歌：{self._last_playback}")
        if self.embedded.state == "启动失败" and self.embedded.recent_logs:
            lines.append(
                "最近 API 输出（脱敏）：\n"
                + "\n".join(list(self.embedded.recent_logs)[-5:])
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("网易云退出登录")
    async def cmd_logout(self, event: AstrMessageEvent):
        """仅退出本插件的本地登录，不撤销 App 设备授权。"""
        error = None
        async with self._login_lock:
            self._login_generation += 1
            if self._captcha_controller:
                self._captcha_controller.stop()
            try:
                self.cookie_file.unlink(missing_ok=True)
            except OSError as e:
                error = str(e)
            else:
                self.api.set_cookie("")
                self._invalid_notice_ts = 0.0
        if error:
            yield event.plain_result(f"退出失败：本地 Cookie 文件无法删除：{error}")
        else:
            yield event.plain_result(
                "已清除扫码/验证码登录的本地 Cookie 和内存登录态。手动网页 Cookie 可在配置页关闭或清空。"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("网易云登录")
    async def cmd_login(self, event: AstrMessageEvent):
        """扫码后先用新 Cookie 独立复核，再持久化登录态。"""
        if not self.api.ncm_api_base:
            yield event.plain_result(
                "请先开启内置服务或配置 ncm_api_base，再使用本命令"
            )
            return
        async with self._login_lock:
            active = self._login_active
            if not active:
                self._login_active = True
                generation = self._login_generation
        if active:
            yield event.plain_result("已有扫码登录进行中，请等待其结束")
            return
        try:
            result = await self._login_flow(event, generation)
        finally:
            async with self._login_lock:
                self._login_active = False
        yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("网易云验证码登录")
    async def cmd_captcha_login(self, event: AstrMessageEvent):
        """仅在管理员私聊会话中获取手机号和短信验证码。"""
        if not event.is_private_chat() or not event.is_admin():
            yield event.plain_result(
                "仅支持管理员私聊操作；不要在群聊发送手机号或验证码"
            )
            return
        command_text = (event.message_str or "").strip()
        if command_text not in ("网易云验证码登录", "/网易云验证码登录"):
            yield event.plain_result(
                "命令不能附带手机号或验证码；请仅发送 /网易云验证码登录"
            )
            return
        if not self.api.ncm_api_base:
            yield event.plain_result("请先开启内置服务或配置 ncm_api_base")
            return
        async with self._login_lock:
            active = self._login_active
            if not active:
                self._login_active = True
                generation = self._login_generation
        if active:
            yield event.plain_result("已有登录进行中，请等待其结束")
            return
        try:
            result = await self._captcha_flow(event, generation)
        finally:
            async with self._login_lock:
                self._login_active = False
        yield event.plain_result(result)

    async def _captcha_flow(self, event: AstrMessageEvent, generation: int) -> str:
        base = self.api.ncm_api_base  # 一次登录固定使用同一服务，后台启动不改变会话。
        sender = event.get_sender_id()
        origin = event.unified_msg_origin
        stage = "phone"
        phone = ""
        attempts = 0
        result = "验证码等待超时；原登录态未更改"
        await event.send(
            event.plain_result(
                "请在此管理员私聊发送绑定网易云账号的中国大陆手机号（默认国家码 86）。"
                "聊天平台可能留存私聊内容，请仅在可信环境使用；发送「取消」结束。"
            )
        )

        @session_waiter(timeout=180)
        async def captcha_waiter(controller: SessionController, ev: AstrMessageEvent):
            nonlocal stage, phone, attempts, result
            self._captcha_controller = controller
            if (
                ev.unified_msg_origin != origin
                or ev.get_sender_id() != sender
                or not ev.is_private_chat()
                or not ev.is_admin()
            ):
                return
            if generation != self._login_generation:
                result = "登录已被退出登录操作取消；原登录态未更改"
                controller.stop()
                return
            ev.stop_event()
            text = (ev.message_str or "").strip()
            if text == "取消":
                result = "已取消验证码登录；原登录态未更改"
                controller.stop()
                return
            if stage == "phone":
                if not re.fullmatch(r"1[3-9][0-9]{9}", text):
                    await ev.send(
                        ev.plain_result(
                            "手机号格式无效；请发送 11 位中国大陆手机号或「取消」"
                        )
                    )
                    return
                now = time.monotonic()
                async with self._login_lock:
                    if generation != self._login_generation:
                        result = "登录已取消；原登录态未更改"
                        controller.stop()
                        return
                    if (
                        now - self._captcha_sent_at.get(sender, -CAPTCHA_COOLDOWN)
                        < CAPTCHA_COOLDOWN
                    ):
                        result = "短信发送过于频繁；请稍后再试，原登录态未更改"
                        controller.stop()
                        return
                    self._captcha_sent_at[sender] = now
                phone = text
                try:
                    await self.api.send_captcha(phone, base=base)
                except Exception:
                    result = (
                        "短信发送失败或受到风控；请稍后在官方客户端确认，原登录态未更改"
                    )
                    controller.stop()
                    return
                if generation != self._login_generation:
                    result = "登录已取消；原登录态未更改"
                    controller.stop()
                    return
                stage = "captcha"
                await ev.send(
                    ev.plain_result(
                        "短信请求已提交；请在此私聊发送验证码（3 分钟内），或发送「取消」。请勿转发验证码。"
                    )
                )
                return
            if not re.fullmatch(r"[0-9]{4,8}", text):
                await ev.send(
                    ev.plain_result("验证码格式无效；请发送 4-8 位数字或「取消」")
                )
                return
            attempts += 1
            try:
                cookie = await self.api.login_cellphone(phone, text, base=base)
            except Exception:
                if attempts >= 2:
                    result = (
                        "验证码登录未成功；已达到重试上限，请停止重试并检查是否受到风控"
                    )
                    controller.stop()
                else:
                    await ev.send(
                        ev.plain_result(
                            "验证码登录未成功；最多再试一次，若受到风控请停止重试"
                        )
                    )
                return
            if generation != self._login_generation:
                result = "登录已取消；原登录态未更改"
                controller.stop()
                return
            candidate = NetEaseAPI(
                proxy=self.cfg.get("http_proxy", ""),
                ncm_api_base=base,
                cookie=cookie,
            )
            try:
                result = await self._save_login(cookie, candidate, generation, "验证码")
            finally:
                await candidate.close()
            controller.stop()

        try:
            await captcha_waiter(event)
        except asyncio.TimeoutError:
            result = "验证码等待超时；原登录态未更改"
        except Exception:
            result = "验证码登录失败；原登录态未更改"
        finally:
            self._captcha_controller = None
            phone = ""
        if generation != self._login_generation:
            return "登录已被退出登录操作取消；原登录态未更改"
        return result

    async def _save_login(
        self, cookie: str, candidate: NetEaseAPI, generation: int, method: str
    ) -> str:
        cookie = normalize_cookie(cookie)
        if not has_music_u(cookie):
            return "新 Cookie 缺少 MUSIC_U 登录凭证（疑似游客态）；原登录态未更改，请重新扫码"
        try:
            valid = await candidate.probe_login(force=True)
        except Exception:
            valid = False
        account = candidate.account
        if not valid or not account.uid:
            reason = account.reason
            if reason == "guest":
                return (
                    "新 Cookie 复核为游客/匿名会话（非账号登录），原登录态未更改；"
                    "请确认网易云 App 上登录的是要绑定的账号后重新扫码"
                )
            if reason == "mismatch":
                return (
                    "新 Cookie 复核发现 account 与 profile 不是同一用户"
                    "（疑似接口缓存串号），原登录态未更改；请稍等片刻后重新扫码"
                )
            if reason == "bad_code":
                return "新 Cookie 未通过独立 UID 复核（接口返回异常）；原登录态未更改，请检查服务后重试"
            return "新 Cookie 未通过独立 UID 复核；原登录态未更改，请检查服务或重试"
        async with self._login_lock:
            if generation != self._login_generation:
                return "登录已被退出登录操作取消；新 Cookie 未保存"
            tmp = self.cookie_file.with_name(f"ncm_cookie_{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(cookie, encoding="utf-8")
                os.replace(tmp, self.cookie_file)
            except OSError:
                logger.warning("[ncm_player] Cookie 保存失败")
                return "新 Cookie 复核通过，但本地保存失败；原登录态未更改"
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    logger.warning("[ncm_player] 临时 Cookie 文件清理失败")
            self.api.adopt_login(candidate)
            self._invalid_notice_ts = 0.0
        # 昵称是公开信息，不打码展示，方便对照 App 确认是否登对账号；
        # userName 可能是手机号，仍打码。
        if account.nickname:
            name = f"，昵称：{account.nickname.strip()}"
        elif account.user_name:
            name = f"，用户名：{_mask_name(account.user_name)}"
        else:
            name = ""
        detail = f"；{account.error}" if account.error else ""
        return (
            f"{method}登录并复核成功：UID {account.uid}{name}；{account.membership}{detail}。"
            "请对照网易云 App 中的 UID；若会员权益不符，排查服务、代理及接口缓存。"
        )

    def _expire_qr(self, path):
        if self._active_qr_path == path:
            self._active_qr_path = None
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("[ncm_player] 二维码临时文件清理失败")

    async def _login_flow(self, event: AstrMessageEvent, generation: int) -> str:
        base = self.api.ncm_api_base
        try:
            key = await self.api.qr_key(base=base)
            qrimg = await self.api.qr_create(key, base=base)
            qr_path = self.cache_dir / f"login_qr_{uuid.uuid4().hex}.png"
            qr_path.write_bytes(base64.b64decode(qrimg.split(",", 1)[1]))
            prev_qr, self._active_qr_path = self._active_qr_path, qr_path
            if prev_qr and prev_qr != qr_path:
                self._expire_qr(prev_qr)
            if generation != self._login_generation:
                self._expire_qr(qr_path)
                return "登录已被退出登录操作取消；请重新发起扫码"
            self._active_qr_path = qr_path
            try:
                await event.send(
                    event.chain_result(
                        [
                            Image.fromFileSystem(str(qr_path)),
                            Plain("请用网易云音乐 App 扫码登录（3 分钟内有效）"),
                        ]
                    )
                )
            finally:
                asyncio.get_running_loop().call_later(240, self._expire_qr, qr_path)
        except Exception as e:
            logger.warning(f"[ncm_player] 生成或发送二维码失败: {e}")
            return "二维码生成或发送失败，请检查 API 服务后重试"

        confirmed = False
        for _ in range(120):
            await asyncio.sleep(1.5)
            if generation != self._login_generation:
                return "登录已被退出登录操作取消；请重新发起扫码"
            try:
                code, cookie = await self.api.qr_check(key, base=base)
            except Exception as e:
                logger.warning(f"[ncm_player] 扫码状态查询失败: {e}")
                continue
            if generation != self._login_generation:
                return "登录已被退出登录操作取消；请重新发起扫码"
            if code == 800:
                return "二维码已过期，请重新发起登录"
            if code == 802 and not confirmed:
                confirmed = True
                await event.send(
                    event.plain_result("扫码成功，请在网易云音乐 App 上点击确认登录")
                )
            if code != 803:
                continue
            if not has_music_u(cookie):
                # 典型坑：服务只下发游客态 Cookie（仅 MUSIC_A），此时扫码看似
                # 成功，复核却得到同一个游客账号。必须拒绝，不能保存。
                logger.warning(
                    "[ncm_player] 扫码已确认，但新 Cookie 缺少 MUSIC_U 登录凭证"
                    f"(cookie_keys={cookie_keys(cookie)})；疑似游客态，原登录态未更改"
                )
                return (
                    "扫码已确认，但服务仅返回游客态 Cookie（缺少 MUSIC_U 登录凭证），"
                    "原登录态未更改；请确认 App 上登录的是要绑定的账号后重新扫码"
                )
            candidate = NetEaseAPI(
                proxy=self.cfg.get("http_proxy", ""),
                ncm_api_base=base,
                cookie=cookie,
            )
            try:
                return await self._save_login(cookie, candidate, generation, "扫码")
            finally:
                await candidate.close()
        return "登录等待超时，请重新发起"
