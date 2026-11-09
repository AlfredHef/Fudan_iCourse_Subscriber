import unittest
from unittest.mock import Mock, patch

import requests

from src.api.webvpn import SchoolLoginActionRequired, WebVPNSession, _login_token
from src.runtime import config


def response(status, location=None):
    result = requests.Response()
    result.status_code = status
    result._content_consumed = True
    if location:
        result.headers["Location"] = location
    return result


class PortalLoginTests(unittest.TestCase):
    def setUp(self):
        self.vpn = WebVPNSession()
        self.get = Mock()
        self.vpn.session.get = self.get

    def test_starts_cas_at_portal_and_follows_relative_redirect(self):
        self.get.side_effect = [
            response(302, "/cas"),
            response(302, config.IDP_BASE + "/ac/#/login?entityId=https%3A%2F%2Fwebvpn.fudan.edu.cn&lck=a%2Bb"),
        ]
        self.assertEqual(self.vpn._get_auth_context(), ("a+b", config.WEBVPN_BASE))
        self.assertEqual([call.args[0] for call in self.get.call_args_list],
                         [config.WEBVPN_BASE + suffix for suffix in ("/login?cas_login=true", "/cas")])

    def test_rejects_foreign_host_and_wrong_context(self):
        for location in ("https://example.org/login", config.IDP_BASE + "/ac/?lck=key&entityId=other"):
            with self.subTest(location=location):
                self.get.reset_mock()
                self.get.return_value = response(302, location)
                with self.assertRaises(RuntimeError):
                    self.vpn._get_auth_context()
                self.get.assert_called_once()

    def test_retries_transient_cas_entry_timeout(self):
        self.get.side_effect = [
            requests.ReadTimeout(),
            response(302, config.IDP_BASE + "/ac/?entityId=" + config.WEBVPN_BASE + "&lck=key"),
        ]
        self.assertEqual(self.vpn._get_auth_context(), ("key", config.WEBVPN_BASE))
        self.assertEqual(self.get.call_args_list[0], self.get.call_args_list[1])

    def test_stops_after_repeated_timeouts(self):
        self.get.side_effect = requests.ReadTimeout()
        with self.assertRaisesRegex(RuntimeError, "webvpn.fudan.edu.cn"):
            self.vpn._get_auth_context()
        self.assertEqual(self.get.call_count, 3)

    def test_limits_redirect_loop(self):
        self.get.return_value = response(302, "/loop")
        with self.assertRaises(RuntimeError):
            self.vpn._get_auth_context()
        self.assertEqual(self.get.call_count, 9)

    def test_callback_timeout_probes_portal_without_replaying_ticket(self):
        self.vpn.session.cookies.set("wengine_vpn_ticket", "example", domain="webvpn.fudan.edu.cn")
        self.get.side_effect = [requests.Timeout(), response(200)]
        self.vpn._establish_session("callback")
        self.assertEqual([call.args[0] for call in self.get.call_args_list],
                         ["callback", config.WEBVPN_BASE + "/"])

    def test_cookie_alone_does_not_verify_session(self):
        self.vpn.session.cookies.set("wengine_vpn_ticket", "example", domain="webvpn.fudan.edu.cn")
        self.get.side_effect = [response(200), response(302, "/login")]
        with self.assertRaises(RuntimeError):
            self.vpn._establish_session("callback")

    @patch("src.api.icourse.ICourseClient.check_alive", return_value=True)
    def test_authenticated_icourse_skips_cas(self, check_alive):
        self.assertTrue(self.vpn.authenticate_icourse())
        self.get.assert_not_called()

class LoginTokenTests(unittest.TestCase):
    def test_token_issued(self):
        self.assertEqual(_login_token({"loginToken": "test", "second": None}), "test")

    def test_second_factor_requires_interaction(self):
        with self.assertRaisesRegex(SchoolLoginActionRequired, "SMS verification"):
            _login_token({"second": True, "moduleCodes": ["userAndSms"], "loginToken": None})

    def test_empty_reply_is_not_a_challenge(self):
        with self.assertRaisesRegex(RuntimeError, "did not issue"):
            _login_token({})


class TotpContinuationTests(unittest.TestCase):
    # RFC 6238 Appendix B, SHA-1 at Unix time 59.
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    challenge = {"code": 200, "second": True, "moduleCodes": ["qr", "userAndOtp"],
                 "requestNumber": "request", "authChainCode": "next", "userName": "student"}

    def test_supported_settings_generate_the_expected_code(self):
        import datetime
        from src.api.webvpn import _current_otp
        with patch("pyotp.totp.datetime.datetime", wraps=datetime.datetime) as dt, patch("src.api.webvpn.time.time", return_value=48):
            dt.now.return_value = datetime.datetime.fromtimestamp(59)
            self.assertEqual(_current_otp(self.seed), "287082")
            self.assertEqual(_current_otp(f"otpauth://totp/test?secret={self.seed}&digits=8"), "94287082")

    def setUp(self):
        self.vpn = WebVPNSession()
        self.post = self.vpn.session.post = Mock()
        self.payload = {"entityId": "service", "lck": "key", "requestType": "chain_type", "authPara": {"password": "encrypted"}}
        self.headers = {"Origin": "portal"}

    def submit(self, result, seed):
        self.post.side_effect = [Mock(json=lambda: self.challenge), Mock(status_code=200, json=lambda: result)]
        with patch.object(config, "ICOURSE_TOTP_SECRET", seed):
            return self.vpn._submit_authentication("endpoint", self.payload, self.headers)

    @patch("src.api.webvpn._current_otp", return_value="012345")
    def test_continuation_uses_original_session_and_server_challenge(self, generate):
        self.assertEqual(self.submit({"code": 200, "loginToken": "token"}, self.seed), "token")
        self.post.assert_called_with("endpoint", json={
            "entityId": "service", "lck": "key", "requestType": "chain_type",
            "authModuleCode": "userAndOtp", "authChainCode": "next", "requestNumber": "request",
            "authPara": {"loginName": "student", "otpCode": "012345"},
        }, headers=self.headers, timeout=60, allow_redirects=False)

    def test_missing_seed_stops_before_otp_submission(self):
        with self.assertRaisesRegex(SchoolLoginActionRequired, "ICOURSE_TOTP_SECRET"):
            self.submit({}, "")
        self.assertEqual(self.post.call_count, 1)

    @patch("src.api.webvpn._current_otp", return_value="012345")
    def test_rejected_otp_stops_without_replay(self, generate):
        with self.assertRaisesRegex(SchoolLoginActionRequired, "did not complete"):
            self.submit({"code": 401}, self.seed)
        self.assertEqual(self.post.call_count, 2)
