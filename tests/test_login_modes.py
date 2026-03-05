import unittest

from bs4 import BeautifulSoup

from extract import _domain_is_allowlisted, extract_loginish_metadata


class LoginModeTests(unittest.TestCase):
    def test_extract_loginish_metadata_marks_js_indicators(self):
        html = """
        <html><body>
          <form action="#" onsubmit="return false;">
            <label for="email">Email</label>
            <input id="email" type="email" placeholder="you@example.com" />
            <input type="password" id="pwd" placeholder="Password" />
            <button type="submit">Sign in</button>
          </form>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        meta = extract_loginish_metadata(soup, "https://example.com/login")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["submit_mode"], "js_handled")
        self.assertIn("blank_or_js_action", meta["js_indicators"])
        self.assertIn("onsubmit_handler", meta["js_indicators"])

    def test_domain_allowlist_exact_and_subdomain(self):
        self.assertTrue(_domain_is_allowlisted("https://app.example.com/login", ["example.com"]))
        self.assertTrue(_domain_is_allowlisted("https://example.com", ["example.com"]))
        self.assertFalse(_domain_is_allowlisted("https://evil-example.com", ["example.com"]))


if __name__ == "__main__":
    unittest.main()
