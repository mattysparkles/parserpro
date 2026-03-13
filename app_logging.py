from datetime import datetime
from traceback import format_exc

from logging import write_detailed_log, write_privacy_log


_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_LOG_ONCE_KEYS = set()


class AppLogger:
    def __init__(self, debug=False):
        self.debug_enabled = bool(debug)

    def set_debug(self, enabled):
        self.debug_enabled = bool(enabled)

    def _should_log(self, level):
        threshold = _LEVELS["DEBUG"] if self.debug_enabled else _LEVELS["INFO"]
        return _LEVELS.get(level, _LEVELS["INFO"]) >= threshold

    def log(self, level, message):
        lvl = (level or "INFO").upper()
        if not self._should_log(lvl):
            return
        ts = datetime.now().strftime("%H:%M:%S")
        text = str(message)
        print(f"[{ts}] {lvl}: {text}")
        write_detailed_log(text, lvl)
        write_privacy_log(text, lvl)

    def debug(self, message):
        self.log("DEBUG", message)

    def info(self, message):
        self.log("INFO", message)

    def warn(self, message):
        self.log("WARN", message)

    def error(self, message):
        self.log("ERROR", message)

    def exception(self, message):
        self.error(f"{message}\n{format_exc()}")

    def log_once(self, key, level, message):
        if key in _LOG_ONCE_KEYS:
            return
        _LOG_ONCE_KEYS.add(key)
        self.log(level, message)


logger = AppLogger()


def set_debug_logging(enabled):
    logger.set_debug(enabled)


def log_once(key, message, level="WARN"):
    logger.log_once(key, level, message)
