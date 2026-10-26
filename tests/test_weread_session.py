import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from core.weread_session import (
    format_cookie,
    parse_cookie,
    renew_cookie,
    renew_persisted_session,
)


class FakeCookieJar:
    def __init__(self, values):
        self._values = dict(values)

    def get_dict(self):
        return dict(self._values)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, cookies=None):
        self.status_code = status_code
        self._payload = payload
        self.cookies = FakeCookieJar(cookies or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


COOKIE = "wr_skey=OLDSKEY; wr_vid=937607486; wr_ql=v1; wr_rt=web%40REFRESH"


class ParseCookieTest(unittest.TestCase):
    def test_parses_fields_in_order(self):
        self.assertEqual(
            parse_cookie(COOKIE),
            {
                "wr_skey": "OLDSKEY",
                "wr_vid": "937607486",
                "wr_ql": "v1",
                "wr_rt": "web%40REFRESH",
            },
        )

    def test_duplicate_key_keeps_first_value(self):
        # 服务端取第一个，去重也必须留第一个，否则会把过期值当有效值送出去
        self.assertEqual(parse_cookie("wr_skey=A; wr_skey=B")["wr_skey"], "A")

    def test_round_trip(self):
        self.assertEqual(format_cookie(parse_cookie(COOKIE)), COOKIE)


class RenewCookieTest(unittest.TestCase):
    def test_success_swaps_skey_and_keeps_other_fields(self):
        response = FakeResponse(payload={"succ": 1}, cookies={"wr_skey": "NEWSKEY"})
        with patch("requests.post", return_value=response):
            renewed = renew_cookie(COOKIE)
        fields = parse_cookie(renewed)
        self.assertEqual(fields["wr_skey"], "NEWSKEY")
        self.assertEqual(fields["wr_vid"], "937607486")
        self.assertEqual(fields["wr_rt"], "web%40REFRESH")

    def test_absorbs_newly_issued_refresh_token(self):
        # 续期可能同时换发 wr_rt，不吸收的话下次就续不动了
        response = FakeResponse(
            payload={"succ": 1},
            cookies={"wr_skey": "NEWSKEY", "wr_rt": "web%40ROTATED"},
        )
        with patch("requests.post", return_value=response):
            renewed = renew_cookie(COOKIE)
        self.assertEqual(parse_cookie(renewed)["wr_rt"], "web%40ROTATED")

    def test_without_refresh_token_returns_empty(self):
        with patch("requests.post") as post:
            self.assertEqual(renew_cookie("wr_skey=OLD; wr_vid=1"), "")
        post.assert_not_called()

    def test_succ_zero_returns_empty(self):
        response = FakeResponse(payload={"succ": 0}, cookies={"wr_skey": "NEWSKEY"})
        with patch("requests.post", return_value=response):
            self.assertEqual(renew_cookie(COOKIE), "")

    def test_missing_new_skey_returns_empty(self):
        # succ=1 但没下发新 skey 就等于没续上，不能当成功
        response = FakeResponse(payload={"succ": 1}, cookies={})
        with patch("requests.post", return_value=response):
            self.assertEqual(renew_cookie(COOKIE), "")

    def test_http_error_returns_empty(self):
        response = FakeResponse(status_code=500, payload={"succ": 1},
                                cookies={"wr_skey": "NEWSKEY"})
        with patch("requests.post", return_value=response):
            self.assertEqual(renew_cookie(COOKIE), "")

    def test_network_error_returns_empty(self):
        with patch("requests.post", side_effect=OSError("boom")):
            self.assertEqual(renew_cookie(COOKIE), "")


class RenewPersistedSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".lic", delete=False,
                                               encoding="utf-8")
        self.tmp.close()
        self.addCleanup(os.unlink, self.tmp.name)

    def write_lic(self, weread_data):
        with open(self.tmp.name, "w", encoding="utf-8") as handle:
            yaml.safe_dump({"weread_data": weread_data}, handle, allow_unicode=True)

    def read_lic(self):
        with open(self.tmp.name, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)["weread_data"]

    def test_persists_renewed_cookie_and_vid(self):
        self.write_lic({"cookie": COOKIE, "cookie_refresh_last_ts": 0})
        response = FakeResponse(payload={"succ": 1}, cookies={"wr_skey": "NEWSKEY"})
        with patch("requests.post", return_value=response):
            result = renew_persisted_session(lic_path=self.tmp.name)
        self.assertTrue(result["renewed"])
        saved = self.read_lic()
        self.assertEqual(parse_cookie(saved["cookie"])["wr_skey"], "NEWSKEY")
        self.assertEqual(saved["vid"], "937607486")
        self.assertGreater(saved["cookie_refresh_last_ts"], 0)

    def test_cooldown_skips_request(self):
        self.write_lic({"cookie": COOKIE, "cookie_refresh_last_ts": time.time()})
        with patch("requests.post") as post:
            result = renew_persisted_session(lic_path=self.tmp.name, min_interval=600)
        self.assertEqual(result["reason"], "cooldown")
        post.assert_not_called()

    def test_force_ignores_cooldown(self):
        self.write_lic({"cookie": COOKIE, "cookie_refresh_last_ts": time.time()})
        response = FakeResponse(payload={"succ": 1}, cookies={"wr_skey": "NEWSKEY"})
        with patch("requests.post", return_value=response):
            result = renew_persisted_session(lic_path=self.tmp.name, force=True)
        self.assertTrue(result["renewed"])

    def test_missing_cookie_reports_no_cookie(self):
        self.write_lic({})
        with patch("requests.post") as post:
            result = renew_persisted_session(lic_path=self.tmp.name)
        self.assertEqual(result["reason"], "no_cookie")
        post.assert_not_called()

    def test_cookie_without_refresh_token_reports_scan_needed(self):
        self.write_lic({"cookie": "wr_skey=OLD; wr_vid=1"})
        with patch("requests.post") as post:
            result = renew_persisted_session(lic_path=self.tmp.name)
        self.assertEqual(result["reason"], "no_refresh_token")
        post.assert_not_called()

    def test_failed_renewal_keeps_old_cookie(self):
        # 续期失败绝不能把坏 Cookie 写回去，原凭证必须原样保留
        self.write_lic({"cookie": COOKIE, "cookie_refresh_last_ts": 0})
        response = FakeResponse(payload={"succ": 0}, cookies={})
        with patch("requests.post", return_value=response):
            result = renew_persisted_session(lic_path=self.tmp.name)
        self.assertEqual(result["reason"], "renew_failed")
        self.assertEqual(self.read_lic()["cookie"], COOKIE)

    def test_renews_each_account_in_multi_account_format(self):
        # 扫码登录写入 accounts 数组并删掉顶层 cookie，续期必须认这个格式，且按 vid 写回各自账号
        other = "wr_skey=OLD2; wr_vid=111; wr_rt=web%40RT2"
        self.write_lic({
            "accounts": [
                {"cookie": COOKIE, "vid": "937607486", "name": "主号"},
                {"cookie": other, "vid": "111", "name": "备用"},
            ],
            "cookie_refresh_last_ts": 0,
        })
        response = FakeResponse(payload={"succ": 1}, cookies={"wr_skey": "NEWSKEY"})
        with patch("requests.post", return_value=response):
            result = renew_persisted_session(lic_path=self.tmp.name)
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["renewed_count"], 2)
        saved = self.read_lic()
        self.assertNotIn("cookie", saved)
        self.assertEqual(len(saved["accounts"]), 2)
        for acc, vid, name in zip(saved["accounts"], ("937607486", "111"), ("主号", "备用")):
            self.assertEqual(parse_cookie(acc["cookie"])["wr_skey"], "NEWSKEY")
            self.assertEqual(parse_cookie(acc["cookie"])["wr_vid"], vid)
            self.assertEqual(acc["name"], name)

    def test_partial_when_some_accounts_fail(self):
        other = "wr_skey=OLD2; wr_vid=111; wr_rt=web%40RT2"
        self.write_lic({
            "accounts": [
                {"cookie": COOKIE, "vid": "937607486"},
                {"cookie": other, "vid": "111"},
            ],
            "cookie_refresh_last_ts": 0,
        })
        responses = [
            FakeResponse(payload={"succ": 1}, cookies={"wr_skey": "NEWSKEY"}),
            FakeResponse(payload={"succ": 0}, cookies={}),
        ]
        with patch("requests.post", side_effect=responses):
            result = renew_persisted_session(lic_path=self.tmp.name)
        self.assertTrue(result["renewed"])
        self.assertEqual(result["reason"], "partial")
        accounts = self.read_lic()["accounts"]
        self.assertEqual(parse_cookie(accounts[0]["cookie"])["wr_skey"], "NEWSKEY")
        self.assertEqual(accounts[1]["cookie"], other)


if __name__ == "__main__":
    unittest.main()
