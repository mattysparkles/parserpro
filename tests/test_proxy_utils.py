import socket
import unittest

from config import normalize_proxy, proxy_is_reachable


class ProxyUtilsTests(unittest.TestCase):
    def test_normalize_proxy_empty_string(self):
        self.assertIsNone(normalize_proxy(""))

    def test_normalize_proxy_string(self):
        self.assertEqual(normalize_proxy("socks5://127.0.0.1:1080"), {"server": "socks5://127.0.0.1:1080"})

    def test_proxy_is_reachable_false_for_unused_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            unused_port = sock.getsockname()[1]

        self.assertFalse(proxy_is_reachable({"server": f"socks5://127.0.0.1:{unused_port}"}, timeout=0.2))


if __name__ == "__main__":
    unittest.main()
