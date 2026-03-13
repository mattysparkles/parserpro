import unittest
from unittest.mock import patch

import fetch


class SeleniumTimeoutHandlingTests(unittest.TestCase):
    @patch("fetch.normalize_and_validate_target", return_value=("https://example.com", None))
    @patch("fetch.get_intercept_proxy", return_value=None)
    @patch("fetch.Options")
    @patch("fetch.webdriver.Chrome")
    def test_selenium_uses_page_load_timeout_and_always_quits(self, chrome_mock, options_mock, _proxy_mock, _normalize_mock):
        driver = chrome_mock.return_value
        driver.page_source = "<html></html>"

        html, err = fetch.fetch_page_selenium("https://example.com")

        self.assertEqual(html, "<html></html>")
        self.assertIsNone(err)
        driver.set_page_load_timeout.assert_called_once_with(180)
        driver.quit.assert_called_once()

    @patch("fetch.normalize_and_validate_target", return_value=("https://example.com", None))
    @patch("fetch.get_intercept_proxy", return_value=None)
    @patch("fetch.Options")
    @patch("fetch.webdriver.Chrome")
    def test_selenium_timeout_returns_fetch_timeout_payload(self, chrome_mock, options_mock, _proxy_mock, _normalize_mock):
        driver = chrome_mock.return_value
        driver.get.side_effect = Exception("timeout while waiting for page load")

        html, err = fetch.fetch_page_selenium("https://example.com")

        self.assertIsNone(html)
        self.assertEqual(err["code"], "fetch_timeout")
        self.assertEqual(err["hint"], "Page load timed out")
        driver.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
