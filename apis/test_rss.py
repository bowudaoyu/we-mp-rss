import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.rss import router


class RssTemplateParamTest(unittest.TestCase):
    """公开 RSS 接口不能把请求里的 template 交给会 eval 的模板引擎。"""

    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    @patch("core.lax.TemplateParser", side_effect=AssertionError("外部模板被渲染"))
    @patch("apis.rss.DB.get_session")
    def test_template_query_param_is_ignored(self, get_session, template_parser):
        session = MagicMock()
        query = session.query.return_value.join.return_value
        query.count.return_value = 0
        query.order_by.return_value.limit.return_value.offset.return_value.all.return_value = []
        get_session.return_value = session

        # 未知 ext 曾经会落到 generate_by_template 分支
        self.client.get(
            "/rss/all",
            params={"ext": "evil", "template": "{{ 1 + 1 }}", "is_update": True},
        )
        template_parser.assert_not_called()

    @patch("apis.rss.DB.get_session")
    def test_normal_rss_still_renders(self, get_session):
        session = MagicMock()
        query = session.query.return_value.join.return_value
        query.count.return_value = 0
        query.order_by.return_value.limit.return_value.offset.return_value.all.return_value = []
        get_session.return_value = session

        response = self.client.get("/rss/all", params={"is_update": True})
        self.assertEqual(response.status_code, 200)
        self.assertIn("<rss", response.text)


if __name__ == "__main__":
    unittest.main()
