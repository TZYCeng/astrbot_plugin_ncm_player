"""Cookie 边界处理与账号快照；不记录任何凭据值。"""

import re
from dataclasses import dataclass

# 扫码接口会把多条 Set-Cookie 用无空格的分号拼在一起。不能将响应属性
# 当成请求 Cookie 原样转发，也不能用 split('=') 截断带填充的 token。
_ATTRIBUTES = {
    "path",
    "domain",
    "expires",
    "max-age",
    "secure",
    "httponly",
    "samesite",
    "priority",
    "partitioned",
    "version",
}
_COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9a-zA-Z-]+$")


def parse_cookie(cookie: str) -> dict[str, str]:
    """兼容浏览器 Cookie 和上游拼接的 Set-Cookie；同名字段取最后一个。"""
    if not isinstance(cookie, str):
        return {}
    value = cookie.strip()
    if value.lower().startswith("cookie:"):
        value = value[7:].strip()
    pairs = {}
    for part in value.split(";"):
        name, sep, item = part.strip().partition("=")
        name, item = name.strip(), item.strip()
        if (
            not sep
            or name.lower() in _ATTRIBUTES
            or not _COOKIE_NAME.fullmatch(name)
            or "\r" in item
            or "\n" in item
        ):
            continue
        pairs[name] = item
    if pairs.get("MUSIC_U"):
        pairs.pop("MUSIC_A", None)  # 登录态不再混入游客凭据。
    return pairs


def normalize_cookie(cookie: str) -> str:
    return "; ".join(f"{key}={value}" for key, value in parse_cookie(cookie).items())


def response_cookie(body_cookie: str, set_cookies: list[str]) -> str:
    """响应体优先；仅用本次响应头补充缺失字段，不使用会话 CookieJar。"""
    pairs = parse_cookie(body_cookie)
    for raw in set_cookies:
        for key, value in parse_cookie(raw.split(";", 1)[0]).items():
            if not pairs.get(key):
                pairs[key] = value
    return normalize_cookie("; ".join(f"{k}={v}" for k, v in pairs.items()))


def has_music_u(cookie: str) -> bool:
    return bool(parse_cookie(cookie).get("MUSIC_U", "").strip('"'))


def cookie_keys(cookie: str) -> list[str]:
    return list(parse_cookie(cookie))


@dataclass(frozen=True)
class AccountState:
    """一次完整复核的不可变快照；网络错误与非会员是不同状态。"""

    login_valid: bool | None = None
    vip: bool | None = None
    membership: str = "会员状态未知"
    uid: str = ""
    nickname: str = ""
    user_name: str = ""
    reason: str = ""
    error: str = ""
    service: str = ""
    checked_at: float = 0.0


def membership_status(payload: dict, profiles: list[dict]) -> tuple[bool | None, str]:
    """只按明确字段判断；未知结构/查询失败不猜成非会员或 SVIP。"""
    data = payload.get("data") if payload.get("code") == 200 else None
    expired = False
    if isinstance(data, dict):
        # /vip/info 返回各会员产品的到期时间，不能只看历史等级。
        import time

        now = time.time() * 1000
        seen = False
        active = []
        for key in ("associator", "musicPackage", "redplus", "redPlus"):
            product = data.get(key)
            if not isinstance(product, dict) or "expireTime" not in product:
                continue
            try:
                expires = float(product["expireTime"] or 0)
            except (TypeError, ValueError):
                continue
            seen = True
            if expires > now:
                active.append(key)
        if active:
            return True, "SVIP" if any(
                k.lower() == "redplus" for k in active
            ) else "VIP"
        expired = seen
    for profile in profiles:
        try:
            if int(profile.get("vipType") or 0) > 0:
                return True, "VIP（账号字段确认）"
        except (TypeError, ValueError):
            continue
    # 部分接口只返回某一产品的过期时间；不能覆盖另一接口明确的有效会员字段。
    if expired:
        return False, "非会员（会员产品已到期）"
    return None, "会员状态未知"
