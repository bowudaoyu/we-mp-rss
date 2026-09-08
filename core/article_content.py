from __future__ import annotations

from typing import Any, Tuple

from core.config import cfg
from core.models.base import DATA_STATUS
from core.print import print_info, print_warning
from core.process_timeout import (
    ProcessExecutionError,
    ProcessExecutionTimeout,
    run_in_process,
)


def normalize_content_mode(mode: str | None = None) -> str:
    normalized = (mode or cfg.get("gather.content_mode", "web") or "web").strip().lower()
    if normalized not in {"web", "api"}:
        return "web"
    return normalized


def extract_origin_article_id(article_id: str, mp_id: str | None = None) -> str:
    if not article_id:
        return ""

    mp_prefix = (mp_id or "").replace("MP_WXS_", "").strip()
    if mp_prefix:
        prefixed = f"{mp_prefix}-"
        if article_id.startswith(prefixed):
            return article_id[len(prefixed):]

    return article_id


def build_article_url(article: Any) -> str:
    article_url = (getattr(article, "url", "") or "").strip()
    if article_url:
        return article_url

    origin_id = extract_origin_article_id(
        getattr(article, "id", ""),
        getattr(article, "mp_id", ""),
    )
    if not origin_id:
        return ""

    return f"https://mp.weixin.qq.com/s/{origin_id}"


# 微信文章页里同时存在多个时间变量，按可信度排序取第一个命中的。
# 两类页面用的变量名不一样：常规图文是 `var ct = "…"`，图片消息
# （itemShowType=8，common_share.html）是 `window.ct = '…'` 和 `ori_create_time: '…'`。
# 两个坑：
#   1. 页面里还有一处压缩后的 `var ct=et(r,G)`（无引号），所以必须限定引号+纯数字；
#   2. 同页的 `svr_time` / `enterid` / `filter_time` 都是“当前时间”，绝不能匹配进来。
_PUBLISH_TIME_PATTERNS = (
    r"var\s+ct\s*=\s*[\"'](\d{9,13})[\"']",
    r"window\.ct\s*=\s*[\"'](\d{9,13})[\"']",
    r"var\s+createTimestamp\s*=\s*[\"'](\d{9,13})[\"']",
    r"create_timestamp\s*:\s*[\"'](\d{9,13})[\"']",
    r"var\s+oriCreateTime\s*=\s*[\"'](\d{9,13})[\"']",
    r"ori_create_time\s*:\s*[\"'](\d{9,13})[\"']",
    r"var\s+create_time\s*=\s*[\"'](\d{9,13})[\"']",
)

# 2010-01-01 ~ 现在+1 天，用来挡掉误匹配到的其它数字
_PUBLISH_TIME_MIN = 1262304000


def extract_publish_timestamp(html: str | None) -> int:
    """从微信文章页 HTML 中解析真实发布时间（Unix 秒）；解析不到返回 0。

    微信读书通道拿不到发布时间，入库时填的是抓取时间；正文页里的 `var ct`
    才是文章真实的发布时间，补抓正文时顺手把它回填回去。
    """
    import re
    import time

    if not html:
        return 0

    upper_bound = int(time.time()) + 86400
    for pattern in _PUBLISH_TIME_PATTERNS:
        match = re.search(pattern, html)
        if not match:
            continue
        value = int(match.group(1))
        if value > 1_000_000_000_000:  # 毫秒时间戳
            value //= 1000
        if _PUBLISH_TIME_MIN <= value <= upper_bound:
            return value
    return 0


def is_usable_article_content(content: str | None) -> bool:
    """Reject WeChat error/shell pages while allowing short media articles."""
    if not (content or "").strip():
        return False

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    text = soup.get_text(" ", strip=True)
    hard_error_markers = (
        "未知错误，请稍后再试",
        "你暂无权限查看此页面内容",
        "当前环境异常，完成验证后即可继续访问",
    )
    if any(marker in text for marker in hard_error_markers):
        return False

    has_wechat_shell = (
        "微信扫一扫可打开此内容" in text
        and "使用完整服务" in text
    )
    meaningful_length = sum(character.isalnum() for character in text)
    if has_wechat_shell and meaningful_length < 300:
        return False

    return bool(text) or soup.find(["img", "video", "audio", "iframe"]) is not None


def _fetch_with_web(url: str) -> Tuple[str, Any]:
    from driver.wxarticle import Web

    result = Web.get_article_content(url) or {}
    return (result.get("content") or "").strip(),result


def _fetch_with_api(url: str) -> Tuple[str, Any]:
    from core.wx.model.api import MpsApi

    fetcher = MpsApi()
    response_html = (fetcher.content_extract(url) or "").strip()
    if not response_html or response_html == "DELETED":
        return response_html, {}

    meta = {"publish_time_exact": extract_publish_timestamp(response_html)}

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(response_html, "html.parser")
    article_body = soup.select_one("#js_content") or soup.select_one("#js_article")
    if article_body is None:
        print_warning("api response does not contain an article body")
        return "", meta
    return str(article_body), meta


def _fetch_article_content_unbounded(
    url: str,
    preferred_mode: str | None = None,
    allow_fallback: bool = True,
) -> Tuple[str, str, str, int]:
    mode = normalize_content_mode(preferred_mode)
    modes = [mode]
    if allow_fallback:
        modes += [item for item in ("web", "api") if item != mode]

    article_type = ""
    publish_time = 0
    last_mode = mode

    for current_mode in modes:
        last_mode = current_mode
        try:
            if current_mode == "api":
                content,result = _fetch_with_api(url)
            else:
                content,result = _fetch_with_web(url)
            article_type = result.get("article_type", "")
            publish_time = int(result.get("publish_time_exact", 0) or 0)
        except Exception as exc:
            print_warning(f"fetch article content failed in {current_mode} mode: {exc}")
            continue

        if content == "DELETED":
            return content, current_mode, article_type, publish_time
        if is_usable_article_content(content):
            return content, current_mode, article_type, publish_time
        if content:
            print_warning(f"ignoring unusable article content from {current_mode} mode")

    return "", last_mode, article_type, publish_time


def fetch_article_content(
    url: str,
    preferred_mode: str | None = None,
    timeout: float | None = None,
) -> Tuple[str, str, str, int]:
    """Fetch article content in an isolated process with a hard timeout.

    返回 (正文, 实际使用的通道, 文章类型, 页面解析出的发布时间)；
    发布时间解析不到时为 0。
    """
    fetch_timeout = float(timeout or cfg.get("gather.content_fetch_timeout", 60) or 60)
    allow_fallback = bool(cfg.get("gather.content_fallback", True))
    return run_in_process(
        _fetch_article_content_unbounded,
        url,
        preferred_mode,
        allow_fallback,
        timeout=fetch_timeout,
    )


def _apply_page_publish_time(article: Any, page_publish_time: int) -> None:
    """用正文页解析出的真实发布时间订正入库时间。

    微信读书通道的封面接口不返回发布时间，入库时只能填抓取时间，
    导致所有文章都显示成“今天发布”。补抓正文本来就要打开文章页，
    顺手把页面里的 `var ct` 回填过来。相差 60 秒以内视为无需订正。
    """
    if not cfg.get("gather.fix_publish_time", True):
        return
    if not page_publish_time:
        return

    current = int(getattr(article, "publish_time", 0) or 0)
    if abs(current - page_publish_time) <= 60:
        return

    article.publish_time = page_publish_time
    print_info(
        f"article {getattr(article, 'id', '')} publish_time "
        f"{current} -> {page_publish_time}（按文章页真实发布时间订正）"
    )


def mark_article_fetch_failed(session, article: Any, reason: str) -> None:
    failures = int(getattr(article, "fix_fail_count", 0) or 0) + 1
    max_failures = int(cfg.get("gather.content_max_failures", 3) or 3)
    has_existing_content = is_usable_article_content(
        getattr(article, "content", "")
    )
    article.fix_fail_count = failures
    article.has_content = 1 if has_existing_content else 0
    if hasattr(article, "fetch_started_at"):
        article.fetch_started_at = None
    article.status = (
        DATA_STATUS.FAILED
        if failures >= max_failures and not has_existing_content
        else DATA_STATUS.ACTIVE
    )
    session.commit()
    print_warning(
        f"article {getattr(article, 'id', '')} content fetch failed "
        f"({failures}/{max_failures}): {reason}"
    )


def sync_article_content(
    session,
    article: Any,
    preferred_mode: str | None = None,
    force: bool = False,
) -> Tuple[bool, str]:
    existing_content = (getattr(article, "content", "") or "").strip()
    if is_usable_article_content(existing_content) and not force:
        if getattr(article, "has_content", 0) == 0:
            print_info(f"article {article.id} already has content, skipping fetch")
            article.has_content = 1
            article.status = DATA_STATUS.ACTIVE
            if hasattr(article, "fetch_started_at"):
                article.fetch_started_at = None
            session.commit()
            session.refresh(article)
            return True, "cached"
        return False, "cached"

    article_url = build_article_url(article)
    if not article_url:
        print_warning(f"article {getattr(article, 'id', '')} has no valid url")
        mark_article_fetch_failed(session, article, "missing_url")
        return False, "missing_url"

    try:
        content, mode, article_type, page_publish_time = fetch_article_content(
            article_url, preferred_mode
        )
    except ProcessExecutionTimeout as exc:
        mark_article_fetch_failed(session, article, str(exc))
        return False, "timeout"
    except ProcessExecutionError as exc:
        mark_article_fetch_failed(session, article, str(exc))
        return False, "process_error"
    except Exception as exc:
        session.rollback()
        mark_article_fetch_failed(session, article, str(exc))
        return False, "error"

    if not content:
        mark_article_fetch_failed(session, article, f"empty response via {mode}")
        return False, mode

    try:
        if content == "DELETED":
            article.content = ""
            article.content_html = ""
            article.status = DATA_STATUS.DELETED
            article.has_content = 0
            if hasattr(article, "fetch_started_at"):
                article.fetch_started_at = None
            session.commit()
            session.refresh(article)
            print_info(f"article {article.id} marked as deleted via {mode}")
            return True, mode

        from driver.wxarticle import Web
        from tools.fix import fix_html

        article.content = content
        article.content_html = fix_html(content)
        article.show_type=article_type or article.show_type
        _apply_page_publish_time(article, page_publish_time)
        article.status = DATA_STATUS.ACTIVE
        article.has_content = 1
        if hasattr(article, "fetch_started_at"):
            article.fetch_started_at = None
        if not (getattr(article, "description", "") or "").strip():
            article.description = Web.get_description(content)
        # 修正成功,重置失败计数
        if hasattr(article, 'fix_fail_count'):
            article.fix_fail_count = 0
        session.commit()
        session.refresh(article)
        print_info(f"article {article.id} content synced via {mode}")
        return True, mode
    except Exception as exc:
        session.rollback()
        mark_article_fetch_failed(session, article, str(exc))
        return False, "save_error"
