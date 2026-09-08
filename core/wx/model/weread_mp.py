"""Collect WeChat Official Account articles through WeRead Web."""

import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from core.log import logger
from core.models.feed import Feed
from core.print import print_info, print_warning
from core.wx.model.weread import MpsWeread


WEREAD_WEB_BASE = "https://weread.qq.com"


class WereadMPAPIError(Exception):
    def __init__(self, code, message, retriable=True):
        super().__init__(f"WeRead MP API error {code}: {message}")
        self.code = code
        self.message = message
        self.retriable = retriable


def build_mp_url(original_id: str) -> str:
    original_id = str(original_id or "").strip()
    if not original_id:
        return ""
    # 微信文章短链 token 是 base64url（A-Za-z0-9_-），本身不含 '~'。
    # WeRead 的 reviewId / originalId 用 '~' 代替了 '_'，原样拼进 URL 会打开一个
    # 31613 字节的错误页（无 og:title、无正文），RSS 链接点不开、正文也永远抓不到。
    # 实测 5 篇：'~' 原样 → 错误页；换成 '_' → 正常文章页（2-3MB）；换成 '-' → 错误页。
    article_token = quote(original_id.replace("~", "_"), safe="")
    return f"https://mp.weixin.qq.com/s/{article_token}"


def build_mp_link_from_review_id(review_id: str, book_id: str = "") -> str:
    """从 WeRead 的 reviewId 推导公众号原文链接。

    reviewId 形如 ``MP_WXS_<bookId>_<articleToken>``，末尾段即为
    mp.weixin.qq.com 原文短链的 token。新版接口 ``/api/mp/cover``
    不返回 originalId，只能由此推导。

    token 里的 ``~`` 必须换回 ``_``：微信短链 token 是 base64url（A-Za-z0-9_-），
    不含 ``~``，那是 WeRead 侧对 ``_`` 的编码。原样拼进 URL 会得到 31613 字节的错误页。
    """
    review_id = str(review_id or "").strip()
    if not review_id:
        return ""
    token = review_id
    prefix = f"{book_id}_" if book_id else ""
    if prefix and review_id.startswith(prefix):
        token = review_id[len(prefix):]
    elif "_" in token:
        token = token.split("_")[-1]
    return f"https://mp.weixin.qq.com/s/{quote(token.replace('~', '_'), safe='')}"


def _raise_response_error(payload: dict):
    code = payload.get("errCode", payload.get("errcode", 0))
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 0:
        return
    message = payload.get("errMsg") or payload.get("errmsg") or str(code)
    raise WereadMPAPIError(
        code,
        message,
        retriable=code not in (-2041, -2012, -2010),
    )


def parse_mp_articles(payload: dict):
    if not isinstance(payload, dict):
        raise WereadMPAPIError("invalid_response", "response is not an object")
    _raise_response_error(payload)

    groups = payload.get("reviews", []) or []
    articles = []
    for group in groups:
        group_time = group.get("createTime", 0)
        for sub_review in group.get("subReviews", []) or []:
            review = sub_review.get("review") or {}
            mp_info = review.get("mpInfo") or {}
            review_id = review.get("reviewId") or sub_review.get("reviewId")
            if not review_id:
                continue
            create_time = review.get("createTime") or group_time or 0
            update_time = mp_info.get("time") or create_time
            articles.append({
                "aid": review_id,
                "id": review_id,
                "title": mp_info.get("title", ""),
                "link": build_mp_url(mp_info.get("originalId", "")),
                "cover": mp_info.get("pic_url", ""),
                "digest": mp_info.get("content") or review.get("content", ""),
                "content": "",
                "create_time": create_time,
                "update_time": update_time,
                "read_num": mp_info.get("readNum", 0),
                "like_num": mp_info.get("likeNum", 0),
                "item_show_type": 0,
            })
    return articles, len(groups)


def extract_mp_content(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    if content is None:
        raise WereadMPAPIError("invalid_content", "article body was not found")
    for element in content.select("script, style"):
        element.decompose()
    return content.decode_contents().strip()


class MpsWereadMP(MpsWeread):
    """WeRead-backed collector for existing ``MP_WXS_*`` feeds."""

    def _get_feed_update_time(self, mp_id: str) -> int:
        from core.db import DB

        session = DB.get_session()
        try:
            row = session.query(Feed.update_time).filter(Feed.id == mp_id).first()
            return int(row[0] or 0) if row else 0
        finally:
            session.close()

    def _get_catchup_page_limit(self, requested_pages: int) -> int:
        from core.config import cfg

        configured_limit = int(cfg.get("weread.mp_max_pages", 20) or 20)
        return max(requested_pages, configured_limit, 1)

    def _get_content_interval(self) -> float:
        from core.config import cfg

        return max(float(cfg.get("weread.content_interval", 2) or 0), 0)

    def _get_page_interval(self) -> float:
        from core.config import cfg

        return max(float(cfg.get("weread.page_interval", 1) or 0), 0)

    def _request_headers(self, include_ticket=False):
        headers = {
            "Cookie": self._weread_cookies,
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://weread.qq.com",
            "Referer": "https://weread.qq.com/",
        }
        if include_ticket and self._weread_ticket:
            # 新版微信读书已弃用 x-wr-ticket，仅需有效 Cookie 即可拉取文章列表；
            # 保留旧逻辑以便兼容旧版微信读书。无 ticket 时不拦截请求。
            headers["x-wr-ticket"] = self._weread_ticket
        return headers

    def _get_mp_articles_page(self, book_id: str, offset=0):
        # 注意：/web/mp/articles 已被新版微信读书废弃（实测恒返回 -2041），
        # 该方法仅保留供旧版兼容与测试使用；生产链路请用 _get_mp_cover。
        try:
            response = requests.get(
                f"{WEREAD_WEB_BASE}/web/mp/articles",
                params={"bookId": book_id, "offset": offset},
                headers=self._request_headers(include_ticket=True),
                proxies=self._get_proxies(),
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise WereadMPAPIError("network_error", str(exc)) from exc
        if response.status_code != 200:
            raise WereadMPAPIError(
                response.status_code,
                f"article list returned HTTP {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WereadMPAPIError("invalid_json", "article list is not JSON") from exc
        if not isinstance(payload, dict):
            raise WereadMPAPIError("invalid_response", "article list is not an object")
        _raise_response_error(payload)
        return payload

    def _get_mp_cover(self, book_id: str) -> dict:
        """获取公众号最新一篇文章（新版微信读书唯一可用入口）。

        返回: {"name", "title", "pic", "reviewId", ...}；无文章或接口异常时抛错。
        """
        try:
            response = requests.get(
                f"{WEREAD_WEB_BASE}/api/mp/cover",
                params={"bookId": book_id},
                headers=self._request_headers(),
                proxies=self._get_proxies(),
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise WereadMPAPIError("network_error", str(exc)) from exc
        if response.status_code != 200:
            raise WereadMPAPIError(
                response.status_code,
                f"mp cover returned HTTP {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WereadMPAPIError("invalid_json", "mp cover is not JSON") from exc
        if not isinstance(payload, dict):
            raise WereadMPAPIError("invalid_response", "mp cover is not an object")
        if not payload.get("reviewId"):
            _raise_response_error(payload)
            raise WereadMPAPIError(
                "empty_cover",
                "公众号未返回最新文章（可能从未在微信读书内发布过文章）",
                retriable=False,
            )
        return payload

    def _is_article_gathered(self, mp_id: str, review_id: str) -> bool:
        """判断 reviewId 是否已入库（跨会话增量，避免重复推送）。

        Article.id 存储格式为 ``{mp_id}-{aid}`` 并去掉 ``MP_WXS_`` 前缀，
        与 ``Db.add_article`` 的归一化规则保持一致。
        """
        if not mp_id or not review_id:
            return False
        from core.db import DB
        from core.models.article import Article

        session = DB.get_session()
        try:
            stored_id = f"{mp_id}-{review_id}".replace("MP_WXS_", "")
            row = session.query(Article.id).filter(
                Article.mp_id == mp_id,
                Article.id == stored_id,
            ).first()
            return row is not None
        except Exception:
            return False
        finally:
            session.close()

    def _get_mp_content(self, review_id: str):
        headers = self._request_headers()
        headers["Accept"] = "text/html,application/xhtml+xml,*/*"
        try:
            response = requests.get(
                f"{WEREAD_WEB_BASE}/web/mp/content",
                params={"reviewId": review_id},
                headers=headers,
                proxies=self._get_proxies(),
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise WereadMPAPIError("network_error", str(exc)) from exc
        if response.status_code != 200:
            raise WereadMPAPIError(
                response.status_code,
                f"article content returned HTTP {response.status_code}",
            )
        return extract_mp_content(response.text)

    def get_Articles(
        self,
        faker_id: str = None,
        Mps_id: str = None,
        Mps_title="",
        CallBack=None,
        start_page: int = 0,
        MaxPage: int = 1,
        interval=10,
        Gather_Content=False,
        Item_Over_CallBack=None,
        Over_CallBack=None,
    ):
        """Collect one existing WeChat feed without changing its RSS identity.

        新版微信读书网页版已废弃 ``/web/mp/articles`` 列表接口（恒返回 -2041），
        前端仅保留 ``/api/mp/cover``（最新一篇）+ ``/web/mp/content``（正文）。
        因此采用「cover 增量」方案：每次只取最新一篇文章，已入库则跳过，
        无法回补历史文章列表（旧版接口才支持）。
        """
        self.articles = []
        self.aids = []
        self.get_token()
        self.start_time = time.time()
        self.response_valid = False
        self.last_error = None
        self._load_weread_auth()

        if not self._weread_cookies:
            raise WereadMPAPIError(
                "missing_cookie",
                "WEREAD_COOKIE is required",
                retriable=False,
            )

        book_id = Mps_id if str(Mps_id or "").startswith("MP_WXS_") else faker_id
        if not str(book_id or "").startswith("MP_WXS_"):
            raise WereadMPAPIError(
                "invalid_book_id",
                "the feed id must use the MP_WXS_ prefix",
                retriable=False,
            )

        # 未在微信读书书架上的公众号自动添加到书架（关注），保证采集可用。
        # 失败仅告警不中断：采集接口会给出最终结果。
        try:
            ok, detail = self.ensure_mp_on_shelf(book_id, Mps_title)
            if ok:
                print_info(f"[{Mps_title}] 书架检查: {detail}")
            else:
                print_warning(f"[{Mps_title}] 书架检查失败: {detail}")
        except Exception as exc:
            print_warning(f"[{Mps_title}] 书架检查异常: {exc}")

        gather_content = bool(Gather_Content or self.Gather_Content)
        content_interval = self._get_content_interval()
        latest_publish_time = 0

        print_info(f"微信读书公众号采集模式: {Mps_title} ({book_id})")
        try:
            cover = self._get_mp_cover(book_id)
            review_id = (cover.get("reviewId") or "").strip()
            title = (cover.get("title") or "").strip()
            self.response_valid = True

            if not review_id:
                raise WereadMPAPIError(
                    "empty_cover",
                    f"公众号「{Mps_title}」暂无文章",
                    retriable=False,
                )

            # 跨会话增量：最新文章已入库则无需重复采集
            if self._is_article_gathered(Mps_id, review_id):
                print_info(f"无新文章：最新《{title}》已在库中，跳过")
                latest_publish_time = self._get_feed_update_time(Mps_id)
            else:
                print_info(f"发现新文章: {title}")
                item = {
                    "aid": review_id,
                    "id": review_id,
                    "title": title,
                    "link": build_mp_link_from_review_id(review_id, book_id),
                    "cover": cover.get("pic") or "",
                    "digest": cover.get("digest") or "",
                    "content": "",
                    "create_time": int(time.time()),
                    "update_time": int(time.time()),
                    "read_num": 0,
                    "like_num": 0,
                    "item_show_type": 0,
                    "copyright_stat": 1,
                    "mp_id": Mps_id,
                }
                if gather_content:
                    if content_interval:
                        time.sleep(content_interval)
                    try:
                        item["content"] = self._get_mp_content(review_id)
                    except WereadMPAPIError as exc:
                        logger.warning(f"微信读书正文获取失败 [{review_id}]: {exc}")
                if CallBack is not None:
                    super().FillBack(
                        CallBack=CallBack,
                        data=item,
                        Ext_Data={"mp_title": Mps_title, "mp_id": Mps_id},
                    )
                if Item_Over_CallBack is not None:
                    Item_Over_CallBack(item)
                latest_publish_time = int(item["update_time"])

            self.update_mps(
                Mps_id,
                Feed(
                    sync_time=int(time.time()),
                    update_time=latest_publish_time or None,
                ),
            )
        except Exception as exc:
            self.last_error = {
                "message": str(exc),
                "code": getattr(exc, "code", "weread_mp_error"),
                "retriable": getattr(exc, "retriable", True),
            }
            super().Over(CallBack=Over_CallBack)
            raise

        super().Over(CallBack=Over_CallBack)
