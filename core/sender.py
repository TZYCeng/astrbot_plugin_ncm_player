"""三级降级发送：语音 → 文件 → 音乐卡片 → 纯文本链接。

要点：aiocqhttp 适配器会把 Record 组件统一转成 base64 塞进 WebSocket，
大文件极易撑爆 ws 帧上限（Max payload size exceeded）。
因此在 aiocqhttp 平台上直接调用 event.bot 的原生 API，
按配置以 本地路径 / URL / base64 三种方式引用音频，绕开强制转码。
"""

import asyncio
import base64
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, Record

from .ncm_api import PlayInfo, Song


class SongSender:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.send_timeout = int(cfg.get("send_timeout", 20))

    # ---------- 平台工具 ----------

    @staticmethod
    def _raw_bot(event: AstrMessageEvent):
        """获取 aiocqhttp 的原生 bot 对象；非该平台返回 None"""
        bot = getattr(event, "bot", None)
        if bot is not None and hasattr(bot, "send_private_msg"):
            return bot
        return None

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        return bool(event.get_group_id())

    async def _bot_send(self, bot, event: AstrMessageEvent, segments: list[dict]):
        if self._is_group(event):
            await bot.send_group_msg(
                group_id=int(event.get_group_id()), message=segments
            )
        else:
            await bot.send_private_msg(
                user_id=int(event.get_sender_id()), message=segments
            )

    # ---------- 语音 ----------

    async def _try_record(
        self, event: AstrMessageEvent, play: PlayInfo, audio_path: str | None
    ) -> None:
        """成功返回 None，失败抛异常"""
        mode = str(self.cfg.get("load_mode", "file"))
        bot = self._raw_bot(event)

        if bot is not None:
            if mode == "url":
                file_field = play.url
            elif mode == "base64":
                if not audio_path:
                    raise RuntimeError("base64 模式需要本地文件，但音频未下载")
                b64 = base64.b64encode(Path(audio_path).read_bytes()).decode()
                file_field = f"base64://{b64}"
            else:  # file
                if not audio_path:
                    raise RuntimeError("本地路径模式需要本地文件，但音频未下载")
                file_field = Path(audio_path).resolve().as_uri()
            seg = {"type": "record", "data": {"file": file_field}}
            await asyncio.wait_for(
                self._bot_send(bot, event, [seg]), timeout=self.send_timeout
            )
            return

        # 非 aiocqhttp 平台：走 AstrBot 标准链路（组件会被转 base64）
        record = (
            Record.fromFileSystem(audio_path)
            if audio_path
            else Record.fromURL(play.url)
        )
        await asyncio.wait_for(
            event.send(event.chain_result([record])), timeout=self.send_timeout
        )

    # ---------- 文件 ----------

    async def _try_file(
        self, event: AstrMessageEvent, song: Song, audio_path: str, ext: str
    ) -> None:
        bot = self._raw_bot(event)
        filename = f"{song.name} - {song.artists}.{ext}"
        if bot is not None and hasattr(bot, "upload_private_file"):
            if self._is_group(event):
                coro = bot.upload_group_file(
                    group_id=int(event.get_group_id()),
                    file=str(Path(audio_path).resolve()),
                    name=filename,
                )
            else:
                coro = bot.upload_private_file(
                    user_id=int(event.get_sender_id()),
                    file=str(Path(audio_path).resolve()),
                    name=filename,
                )
            await asyncio.wait_for(coro, timeout=self.send_timeout)
            return
        raise RuntimeError("当前平台不支持直接发送文件")

    # ---------- 音乐卡片 ----------

    async def _try_card(self, event: AstrMessageEvent, song: Song) -> None:
        bot = self._raw_bot(event)
        if bot is None:
            raise RuntimeError("当前平台不支持音乐卡片")
        seg = {
            "type": "music",
            "data": {"type": "163", "id": str(song.id)},
        }
        await asyncio.wait_for(self._bot_send(bot, event, [seg]), timeout=15)

    # ---------- 入口：按发送模式分发 ----------

    async def send(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: PlayInfo,
        audio_path: str | None,
    ) -> str:
        """按配置的 send_mode 发送，返回实际使用的方式描述。

        auto: 语音 → 文件 → 卡片 → 链接 逐级降级
        其他: 只发选定的方式（voice/file/card 的 _ 组合），全部失败时链接保底
        """
        mode = str(self.cfg.get("send_mode", "auto"))
        if mode == "auto":
            return await self._send_auto(event, song, play, audio_path)

        parts = mode.split("_")
        succeeded: list[str] = []
        errors: list[str] = []

        if "voice" in parts:
            try:
                await self._try_record(event, play, audio_path)
                succeeded.append("语音")
            except Exception as e:
                errors.append(f"语音发送失败: {e}")
                logger.warning(f"[ncm_player] 语音发送失败: {e}")
        if "file" in parts:
            if audio_path:
                try:
                    await self._try_file(event, song, audio_path, play.ext)
                    succeeded.append("文件")
                except Exception as e:
                    errors.append(f"文件发送失败: {e}")
                    logger.warning(f"[ncm_player] 文件发送失败: {e}")
            else:
                errors.append("文件发送跳过: 无本地音频（url 载入模式下不下载）")
                logger.warning("[ncm_player] 文件发送跳过：无本地音频")
        if "card" in parts:
            try:
                await self._try_card(event, song)
                succeeded.append("卡片")
            except Exception as e:
                errors.append(f"卡片发送失败: {e}")
                logger.warning(f"[ncm_player] 卡片发送失败: {e}")

        if succeeded:
            return "+".join(succeeded)

        # 全部失败 → 链接保底
        await event.send(
            event.chain_result(
                [Plain(f"🎵 {song.name} - {song.artists}\n{song.page_url}")]
            )
        )
        for err in errors:
            logger.error(f"[ncm_player] {err}")
        return "链接（所选方式均失败，已降级）"

    async def _send_auto(
        self,
        event: AstrMessageEvent,
        song: Song,
        play: PlayInfo,
        audio_path: str | None,
    ) -> str:
        """语音→文件→卡片→链接 逐级降级"""
        errors: list[str] = []

        # 1. 语音
        try:
            await self._try_record(event, play, audio_path)
            return "语音"
        except Exception as e:
            errors.append(f"语音发送失败: {e}")
            logger.warning(f"[ncm_player] 语音发送失败，降级为文件: {e}")

        # 2. 文件（需要本地音频）
        if audio_path:
            try:
                await self._try_file(event, song, audio_path, play.ext)
                return "文件"
            except Exception as e:
                errors.append(f"文件发送失败: {e}")
                logger.warning(f"[ncm_player] 文件发送失败，降级为卡片: {e}")

        # 3. 音乐卡片
        try:
            await self._try_card(event, song)
            return "卡片"
        except Exception as e:
            errors.append(f"卡片发送失败: {e}")
            logger.warning(f"[ncm_player] 卡片发送失败，降级为链接: {e}")

        # 4. 纯文本链接（保底，必定可达）
        await event.send(
            event.chain_result(
                [Plain(f"🎵 {song.name} - {song.artists}\n{song.page_url}")]
            )
        )
        for err in errors:
            logger.error(f"[ncm_player] {err}")
        return "链接"
