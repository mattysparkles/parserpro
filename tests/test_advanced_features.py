import unittest
from unittest.mock import patch

import fetch
from config import load_config


class AdvancedFeatureTests(unittest.TestCase):
    def test_config_defaults_include_advanced_extraction_mode(self):
        cfg = load_config()
        self.assertIn("advanced_extraction_mode", cfg)

    def test_fetch_user_agent_pool_size(self):
        self.assertGreaterEqual(len(fetch.FETCH_USER_AGENTS), 5)

    @patch("fetch.normalize_and_validate_target", return_value=("https://example.com", None))
    @patch("fetch.get_intercept_proxy", return_value=None)
    def test_requests_403_uses_cloudscraper_when_enabled(self, _proxy, _normalize):
        if not fetch.HAS_REQUESTS:
            self.skipTest("requests unavailable")

        class Resp:
            def __init__(self, code, text=""):
                self.status_code = code
                self.text = text

        with patch("fetch.requests.get", return_value=Resp(403, "forbidden")):
            with patch("fetch.HAS_CLOUDSCRAPER", True):
                with patch("fetch.cloudscraper", create=True) as cloudscraper_mod:
                    scraper_factory = cloudscraper_mod.create_scraper
                    scraper = scraper_factory.return_value
                    scraper.get.return_value = Resp(200, "ok")
                    html, err = fetch.fetch_page_requests("https://example.com")
                    self.assertEqual(html, "ok")
                    self.assertIsNone(err)
                    scraper.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
