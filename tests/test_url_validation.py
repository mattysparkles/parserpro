import unittest

from extract import normalize_form_action
from helpers import normalize_and_validate_target, validate_url


class UrlValidationTests(unittest.TestCase):
    def test_validate_url_rejects_obvious_garbage(self):
        self.assertIsNone(validate_url("{ major:0, minor:1 }"))
        self.assertIsNone(validate_url("toString:function(){}"))

    def test_validate_url_adds_https_for_hostnames(self):
        self.assertEqual(validate_url("example.com/login"), "https://example.com/login")

    def test_normalize_form_action_defaults_to_page_url(self):
        page = "https://example.com/login"
        self.assertEqual(normalize_form_action(page, ""), page)

    def test_normalize_form_action_resolves_relative(self):
        self.assertEqual(
            normalize_form_action("https://example.com/login", "/auth"),
            "https://example.com/auth",
        )

    def test_normalize_and_validate_target_rejects_nonstandard_port_by_default(self):
        url, reason = normalize_and_validate_target("eth.2miners.com:2020")
        self.assertIsNone(url)
        self.assertIn("nonstandard port", reason)

    def test_normalize_and_validate_target_adds_https(self):
        url, reason = normalize_and_validate_target("example.com")
        self.assertEqual(url, "https://example.com")
        self.assertIsNone(reason)

    def test_normalize_and_validate_target_rejects_garbage(self):
        url, reason = normalize_and_validate_target("{major:0,...}")
        self.assertIsNone(url)
        self.assertIn("invalid", reason)


if __name__ == "__main__":
    unittest.main()
