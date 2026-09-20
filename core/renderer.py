"""图片渲染：CD 光盘美学风格（浅色磨砂底 + 彩虹光泽盘面）。

参考设计：透明 CD 盒 + 虹彩盘面，封面嵌于盘心。
- render_selection: 选歌列表（每行一张小 CD）
- render_playing:   播放卡片（大 CD + 歌曲信息）
- render_comment:   热评卡片
"""

import glob
import math
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from astrbot.api import logger

from .ncm_api import Comment, Song

# 配色：浅色磨砂系
ACCENT = (225, 45, 60)          # 网易云红
BG_TOP = (247, 249, 252)
BG_BOTTOM = (232, 237, 244)
CARD_WHITE = (255, 255, 255)
BORDER = (222, 227, 235)
INK = (26, 26, 31)              # 主文字
GRAY = (122, 127, 138)          # 次要文字
LIGHT_GRAY = (165, 170, 180)

_FONT_CANDIDATES = [
    "/usr/share/fonts/**/NotoSansCJK*Regular*.ttc",
    "/usr/share/fonts/**/NotoSansSC*.otf",
    "/usr/share/fonts/**/NotoSansCJK*.ttc",
    "/usr/share/fonts/**/wqy*.ttc",
    "/usr/share/fonts/**/SourceHan*.otf",
    "/usr/share/fonts/**/*Hei*.ttf",
    "/usr/share/fonts/**/*Hei*.ttc",
    "/usr/share/fonts/**/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]
_BOLD_CANDIDATES = [
    "/usr/share/fonts/**/NotoSansCJK*Bold*.ttc",
    "/usr/share/fonts/**/NotoSansSC*Bold*.otf",
    "/usr/share/fonts/**/wqy*.ttc",
    "/usr/share/fonts/**/SourceHan*Bold*.otf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

_font_path: str | None = None
_bold_path: str | None = None


def _find_font(candidates: list[str]) -> str | None:
    for pattern in candidates:
        hits = sorted(glob.glob(pattern, recursive=True))
        if hits:
            return hits[0]
    return None


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    global _font_path, _bold_path
    if _font_path is None:
        _font_path = _find_font(_FONT_CANDIDATES)
    if _bold_path is None:
        _bold_path = _find_font(_BOLD_CANDIDATES) or _font_path
    path = _bold_path if bold else _font_path
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            logger.warning(f"[ncm_player] 加载字体失败 {path}: {e}")
    return ImageFont.load_default()


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """按像素宽度折行"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n" or draw.textlength(cur + ch, font=font) > max_w:
            if cur:
                lines.append(cur)
            cur = "" if ch == "\n" else ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _v_gradient(w: int, h: int, top, bottom) -> Image.Image:
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        base.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return base.resize((w, h))


def _load_square(img_bytes: bytes | None, size: int, fallback=(226, 230, 238)) -> Image.Image:
    if img_bytes:
        try:
            img = Image.open(BytesIO(img_bytes)).convert("RGB")
            return ImageOps.fit(img, (size, size), Image.LANCZOS)
        except Exception:
            pass
    return Image.new("RGB", (size, size), fallback)


def _make_disc(cover_bytes: bytes | None, size: int, hole_ratio: float = 0.045) -> Image.Image:
    """把封面渲染成一张 CD 光盘：圆形盘面 + 彩虹光泽 + 模糊盘心 + 中心孔"""
    S = size
    base = _load_square(cover_bytes, S)
    disc = base.convert("RGBA")

    # 轻微压暗盘面，让光泽更明显
    dim = Image.new("RGBA", (S, S), (0, 0, 0, 28))
    disc = Image.alpha_composite(disc, dim)

    # 彩虹光泽：斜向色带，旋转后裁回
    big = int(S * 1.5)
    sheen = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    bands = [
        (0.10, 0.24, (255, 130, 170, 42)),
        (0.24, 0.32, (255, 225, 140, 30)),
        (0.32, 0.46, (130, 200, 255, 40)),
        (0.46, 0.55, (200, 160, 255, 28)),
        (0.66, 0.78, (255, 255, 255, 34)),
    ]
    for t0, t1, color in bands:
        y0, y1 = int(big * t0), int(big * t1)
        sd.rectangle((0, y0, big, y1), fill=color)
    sheen = sheen.rotate(32, resample=Image.BICUBIC, expand=False)
    left = (big - S) // 2
    sheen = sheen.crop((left, left, left + S, left + S))
    disc = Image.alpha_composite(disc, sheen)

    draw = ImageDraw.Draw(disc)
    cx = S / 2
    # 外缘细环
    draw.ellipse((1, 1, S - 1, S - 1), outline=(255, 255, 255, 90), width=2)
    # 盘毂：取封面中心区域高斯模糊 + 轻压暗，而非纯黑
    hub_r = S * 0.19
    hub_d = int(hub_r * 2)
    c0 = int(cx - hub_r)
    hub = base.crop((c0, c0, c0 + hub_d, c0 + hub_d))
    hub = hub.filter(ImageFilter.GaussianBlur(max(2, S // 60))).convert("RGBA")
    hub = Image.alpha_composite(hub, Image.new("RGBA", (hub_d, hub_d), (0, 0, 0, 80)))
    hub_mask = Image.new("L", (hub_d, hub_d), 0)
    ImageDraw.Draw(hub_mask).ellipse((0, 0, hub_d, hub_d), fill=255)
    disc.paste(hub, (c0, c0), hub_mask)
    # 毂环高光线
    for rr, alpha in ((hub_r, 130), (hub_r * 0.72, 70)):
        draw.ellipse((cx - rr, cx - rr, cx + rr, cx + rr), outline=(255, 255, 255, alpha), width=2)
    # 银色内圈
    sil_r = S * 0.075
    draw.ellipse((cx - sil_r, cx - sil_r, cx + sil_r, cx + sil_r), fill=(208, 212, 218, 255))
    draw.ellipse((cx - sil_r, cx - sil_r, cx + sil_r, cx + sil_r), outline=(160, 166, 175, 255), width=2)
    # 中心孔
    hole_r = S * hole_ratio
    draw.ellipse((cx - hole_r, cx - hole_r, cx + hole_r, cx + hole_r), fill=(30, 30, 36, 255))

    # 圆形蒙版
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, S, S), fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(disc, (0, 0), mask)
    return out


def _paste_shadow(base: Image.Image, fg: Image.Image, pos: tuple[int, int], blur: int = 18, alpha: int = 60):
    """给圆形元素加柔和投影"""
    x, y = pos
    sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
    m = Image.new("L", (fg.size[0], fg.size[1]), 0)
    ImageDraw.Draw(m).ellipse((0, 0, fg.size[0], fg.size[1]), fill=alpha)
    sh.paste((40, 45, 60), (x, y + blur // 2), m)
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(sh)
    base.alpha_composite(fg, (x, y))


class CardRenderer:
    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 选歌列表 ----------

    def render_selection(
        self, songs: list[Song], covers: list[bytes | None], keyword: str
    ) -> str:
        W, header_h, row_h, pad = 780, 96, 116, 22
        footer_h = 62
        H = header_h + row_h * len(songs) + footer_h + pad

        bg = _v_gradient(W, H, BG_TOP, BG_BOTTOM).convert("RGBA")
        draw = ImageDraw.Draw(bg)
        f_title = _load_font(32, bold=True)
        f_name = _load_font(26, bold=True)
        f_sub = _load_font(20)
        f_idx = _load_font(24, bold=True)
        f_dur = _load_font(19)

        # 头部：红色竖条 + 标题
        draw.rounded_rectangle((pad, 30, pad + 8, 30 + 40), 4, fill=ACCENT)
        draw.text(
            (pad + 22, 51),
            _ellipsize(draw, f"网易云点歌 · {keyword}", f_title, W - pad * 2 - 40),
            font=f_title, fill=INK, anchor="lm",
        )
        draw.line((pad, header_h - 8, W - pad, header_h - 8), fill=BORDER, width=2)

        for i, song in enumerate(songs):
            y = header_h + i * row_h
            # 白色行卡片
            draw.rounded_rectangle(
                (pad, y + 9, W - pad, y + row_h - 9), 18,
                fill=CARD_WHITE, outline=BORDER, width=1,
            )
            # 小 CD
            thumb = _make_disc(covers[i] if i < len(covers) else None, 92)
            bg.alpha_composite(thumb, (pad + 12, y + 12))
            # 序号圆
            cx, cy, r = pad + 12 + 92 + 34, y + row_h // 2, 17
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT)
            draw.text((cx, cy - 1), str(i + 1), font=f_idx, fill=(255, 255, 255), anchor="mm")
            # 文本
            tx = cx + r + 16
            max_tw = W - tx - pad - 78
            draw.text(
                (tx, y + 34),
                _ellipsize(draw, song.name, f_name, max_tw),
                font=f_name, fill=INK,
            )
            sub = f"{song.artists} · {song.album}" if song.album else song.artists
            draw.text(
                (tx, y + 72),
                _ellipsize(draw, sub, f_sub, max_tw),
                font=f_sub, fill=GRAY,
            )
            draw.text(
                (W - pad - 18, y + row_h // 2),
                song.duration_str, font=f_dur, fill=LIGHT_GRAY, anchor="rm",
            )

        draw.text(
            (W // 2, H - footer_h // 2 - 2),
            "回复序号点歌，例如：1", font=f_sub, fill=LIGHT_GRAY, anchor="mm",
        )

        path = self.out_dir / "selection.png"
        bg.convert("RGB").save(path, "PNG")
        return str(path)

    # ---------- 播放卡片 ----------

    def render_playing(self, song: Song, cover: bytes | None, quality: str) -> str:
        W, H, pad = 980, 520, 36
        bg = _v_gradient(W, H, BG_TOP, BG_BOTTOM).convert("RGBA")
        draw = ImageDraw.Draw(bg)
        f_title = _load_font(40, bold=True)
        f_sub = _load_font(26)
        f_meta = _load_font(21)
        f_tag = _load_font(19, bold=True)
        f_brand = _load_font(20, bold=True)

        # 大 CD（带投影），盘面缩小并垂直居中
        disc_size = int((H - pad * 2) * 0.82)
        disc = _make_disc(cover, disc_size)
        disc_y = (H - disc_size) // 2
        _paste_shadow(bg, disc, (pad + 12, disc_y))

        # 右侧信息区
        tx = pad + 12 + disc_size + 44
        max_tw = W - tx - pad
        draw.rounded_rectangle((tx, pad + 20, tx + 8, pad + 20 + 44), 4, fill=ACCENT)
        draw.text(
            (tx + 24, pad + 42),
            _ellipsize(draw, song.name, f_title, max_tw - 24),
            font=f_title, fill=INK, anchor="lm",
        )
        draw.text(
            (tx, pad + 116),
            _ellipsize(draw, song.artists, f_sub, max_tw),
            font=f_sub, fill=GRAY,
        )
        meta = " · ".join(x for x in [song.album, song.duration_str] if x)
        draw.text(
            (tx, pad + 164),
            _ellipsize(draw, meta, f_meta, max_tw),
            font=f_meta, fill=LIGHT_GRAY,
        )
        # 音质胶囊
        tag = f"♪ {quality}"
        tw = draw.textlength(tag, font=f_tag)
        draw.rounded_rectangle(
            (tx, pad + 214, tx + tw + 34, pad + 214 + 40), 20, outline=ACCENT, width=2
        )
        draw.text((tx + 17, pad + 214 + 20), tag, font=f_tag, fill=ACCENT, anchor="lm")

        # 品牌角标
        draw.text(
            (W - pad, H - pad + 4), "NetEase Cloud Music",
            font=f_brand, fill=LIGHT_GRAY, anchor="rs",
        )

        path = self.out_dir / "playing.png"
        bg.convert("RGB").save(path, "PNG")
        return str(path)

    # ---------- 热评卡片 ----------

    def render_comment(
        self, comment: Comment, song: Song, avatar: bytes | None
    ) -> str:
        W, pad = 780, 34
        f_body = _load_font(26)
        f_sub = _load_font(21)
        f_brand = _load_font(18)

        tmp = Image.new("RGB", (W, 100))
        td = ImageDraw.Draw(tmp)
        lines = _wrap_text(td, comment.content, f_body, W - pad * 2 - 10)
        body_h = len(lines) * 42
        H = pad + 60 + body_h + 30 + 64 + pad + 30

        bg = _v_gradient(W, H, BG_TOP, BG_BOTTOM).convert("RGBA")
        draw = ImageDraw.Draw(bg)

        # 白色卡体
        draw.rounded_rectangle(
            (pad // 2, pad // 2, W - pad // 2, H - pad // 2), 22,
            fill=CARD_WHITE, outline=BORDER, width=1,
        )
        # 引号（手绘双平行四边形，避免字体缺字形）
        qx, qy = pad + 6, pad + 6
        for dx in (0, 30):
            draw.polygon(
                [
                    (qx + dx, qy + 34),
                    (qx + dx + 15, qy),
                    (qx + dx + 31, qy),
                    (qx + dx + 16, qy + 34),
                ],
                fill=ACCENT,
            )
        # 评论正文
        y = pad + 60
        for line in lines:
            draw.text((pad + 6, y), line, font=f_body, fill=INK)
            y += 42

        # 底部分隔 + 用户信息
        y += 16
        draw.line((pad + 6, y, W - pad - 6, y), fill=BORDER, width=2)
        y += 18
        av_size = 56
        avatar_img = _load_square(avatar, av_size).convert("RGBA")
        mask = Image.new("L", (av_size, av_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, av_size, av_size), fill=255)
        circ = Image.new("RGBA", (av_size, av_size), (0, 0, 0, 0))
        circ.paste(avatar_img, (0, 0), mask)
        bg.alpha_composite(circ, (pad + 6, y))
        draw.text(
            (pad + 6 + av_size + 14, y + 14),
            _ellipsize(draw, comment.nickname, f_sub, 320),
            font=f_sub, fill=GRAY,
        )
        draw.text(
            (pad + 6 + av_size + 14, y + 38),
            f"来自《{song.name}》的网易云热评", font=f_brand, fill=LIGHT_GRAY,
        )
        if comment.liked_count:
            draw.text(
                (W - pad - 6, y + av_size // 2),
                f"♥ {comment.liked_count}", font=f_sub, fill=ACCENT, anchor="rm",
            )

        path = self.out_dir / "comment.png"
        bg.convert("RGB").save(path, "PNG")
        return str(path)
