import unittest

from fetch import classify_nav_error
from helpers import normalize_and_validate_target
from extract import normalize_form_action


class FetchErrorClassificationTests(unittest.TestCase):
    def test_error_codes(self):
        self.assertEqual(classify_nav_error("net::ERR_NAME_NOT_RESOLVED"), ("dns_failed", "DNS resolution failed"))
        self.assertEqual(classify_nav_error("ERR_CONNECTION_CLOSED"), ("conn_closed", "Connection closed by peer or non-web endpoint"))
        self.assertEqual(classify_nav_error("ERR_SSL_VERSION_OR_CIPHER_MISMATCH"), ("tls_mismatch", "TLS handshake failed (proxy/AV may interfere)"))
        self.assertEqual(classify_nav_error("ERR_CERT_AUTHORITY_INVALID"), ("cert_invalid", "Untrusted certificate (MITM/captive portal)"))
        self.assertEqual(classify_nav_error("ERR_SOCKS_CONNECTION_FAILED"), ("proxy_down", "SOCKS proxy unreachable"))
        self.assertEqual(classify_nav_error("some random crash"), ("fetch_failed", "Navigation failed"))

    def test_target_validation_rejects_newline_and_non_http(self):
        self.assertEqual(normalize_and_validate_target("example.com\nabc")[0], None)
        self.assertEqual(normalize_and_validate_target("ftp://example.com")[0], None)

    def test_action_urljoin_blank_uses_page(self):
        self.assertEqual(normalize_form_action("https://example.com/login", "#"), "https://example.com/login")


if __name__ == "__main__":
    unittest.main()
