"""微信读书会话保活：用 wr_rt 换新的 wr_skey（纯 HTTP，容器内即可执行）。

为什么需要它：

- ``wr_skey`` 是小时级的短效凭证。过期后 ``/web/shelf/*``、``/web/mp/articles``
  这些要求完整登录态的接口会返回 ``-2012 登录超时``。
- 但采集主力接口 ``/api/mp/cover`` **不校验 wr_skey**，过期后照样返回数据。
  于是"会话已经半死"在采集侧完全看不出来，一直到 ``wr_rt`` 也失效、整个会话
  彻底作废才会断采——这正是断采很久都没人发现的根因。
- 微信读书自己的续期接口 ``POST /web/login/renewal`` 用 Cookie 里的 ``wr_rt``
  换一个新的 ``wr_skey``，不需要浏览器、不需要扫码，容器内一个 HTTP 请求即可。

所以每次采集前续一次，会话就能长期存活。

与 ``core/weread_cookie_refresh.py`` 的分工：那个模块驱动宿主机的真实 Chrome，
负责**首次扫码**和登录态彻底失效后的恢复（重，且依赖宿主机环境）；本模块只做
日常保活。能续期就不必惊动浏览器，续不动了才需要回到扫码流程。
"""
from __future__ import annotations

import time
from typing import Dict

WEREAD_WEB_BASE = "https://weread.qq.com"
RENEWAL_URL = f"{WEREAD_WEB_BASE}/web/login/renewal"

# 续期请求体里的 rq 是"续期后要回跳的接口路径"，微信读书前端固定传这个值，
# 服务端只做存在性校验，不影响换发结果。
_RENEWAL_BODY = {"rq": "%2Fweb%2Fbook%2Fread"}

# 一次采集续一次就够，避免同一批任务里反复打续期接口触发风控。
DEFAULT_MIN_INTERVAL = 600.0

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def parse_cookie(cookie: str) -> Dict[str, str]:
    """把 Cookie 串解析成有序字典；重复键保留**首次**出现的值。

    与 ``weread_cookie_refresh._dedupe_cookie`` 的取舍保持一致：服务端取第一个，
    所以去重时也必须留第一个，否则会把过期值当成有效值送出去。
    """
    fields: Dict[str, str] = {}
    for item in (cookie or "").split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        key, _, value = item.partition("=")
        key = key.strip()
        if key and key not in fields:
            fields[key] = value.strip()
    return fields


def format_cookie(fields: Dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in fields.items())


def renew_cookie(
    cookie: str,
    user_agent: str = "",
    proxies=None,
    timeout=(10, 30),
) -> str:
    """换一个新的 wr_skey，返回续期后的完整 Cookie 串；失败返回空字符串。

    失败一律返回 ""（而不是抛错或返回原串），调用方据此决定是否回落到扫码流程；
    返回原串会让调用方误以为续期成功，把过期凭证又写回 wx.lic。
    """
    import requests

    fields = parse_cookie(cookie)
    if not fields.get("wr_rt"):
        # 没有 refresh token 就换不出新 skey，只能回到扫码流程。
        return ""

    headers = {
        "User-Agent": user_agent or _DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": WEREAD_WEB_BASE,
        "Referer": f"{WEREAD_WEB_BASE}/",
    }
    try:
        response = requests.post(
            RENEWAL_URL,
            json=_RENEWAL_BODY,
            headers=headers,
            cookies=fields,
            proxies=proxies,
            timeout=timeout,
        )
    except Exception:
        return ""

    if response.status_code != 200:
        return ""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    # 正常是 {"succ":1}；明确给了失败标记就不要再吸收响应里的 Cookie。
    if isinstance(payload, dict) and payload.get("succ") in (0, False):
        return ""

    issued = response.cookies.get_dict() if response.cookies else {}
    if not issued.get("wr_skey"):
        # 没换到新 skey 就等于没续上，哪怕 succ=1 也不能当成功。
        return ""

    # 续期可能同时下发新的 wr_rt / wr_gid，一并吸收，否则下次就续不动了。
    fields.update(issued)
    return format_cookie(fields)


def renew_persisted_session(
    lic_path: str = "",
    user_agent: str = "",
    min_interval: float = DEFAULT_MIN_INTERVAL,
    force: bool = False,
) -> dict:
    """读取 wx.lic 里的 Cookie 做一次续期并写回；多账号时逐个续期。

    返回 ``{"renewed": bool, "reason": str, "renewed_count": int, "total": int}``：

    - ``reason="ok"``：所有带 wr_rt 的账号都续上了，新 Cookie 已落盘；
    - ``reason="partial"``：部分账号续上了，其余多半整个会话已失效；
    - ``reason="cooldown"``：距上次刷新不足 ``min_interval`` 秒，本次跳过；
    - ``reason="no_cookie"`` / ``"no_refresh_token"``：还没扫过码，需要走扫码流程；
    - ``reason="renew_failed"``：续期被服务端拒绝，多半是整个会话已失效。
    """
    from core.weread_cookie_refresh import (
        DEFAULT_LIC_PATH,
        _account_entries,
        _load_weread_data,
        _save_cookie,
    )

    path = lic_path or DEFAULT_LIC_PATH
    _, data = _load_weread_data(path)
    cookies = [(a.get("cookie") or "").strip() for a in _account_entries(data)]
    cookies = [c for c in cookies if c]
    result = {"renewed": False, "reason": "", "renewed_count": 0, "total": len(cookies)}
    if not cookies:
        return {**result, "reason": "no_cookie"}
    renewable = [c for c in cookies if parse_cookie(c).get("wr_rt")]
    if not renewable:
        return {**result, "reason": "no_refresh_token"}

    # 冷却复用 cookie_refresh_last_ts：无论上次是浏览器刷新还是 HTTP 续期写的，
    # 语义都是"Cookie 刚更新过"，此时再续一次没有意义。
    last_ts = float(data.get("cookie_refresh_last_ts") or 0)
    if not force and min_interval > 0 and time.time() - last_ts < min_interval:
        return {**result, "reason": "cooldown"}

    renewed_count = 0
    for cookie in renewable:
        new_cookie = renew_cookie(cookie, user_agent=user_agent)
        if not new_cookie:
            continue
        # _save_cookie 按 vid 写回对应账号，并刷新 cookie_refresh_last_ts（即冷却起点）。
        _save_cookie(new_cookie, lic_path=path)
        renewed_count += 1

    if not renewed_count:
        return {**result, "reason": "renew_failed"}
    reason = "ok" if renewed_count == len(renewable) else "partial"
    return {**result, "renewed": True, "reason": reason, "renewed_count": renewed_count}
