"""Redact the infrastructure token even in third-party exception formatting."""

import logging
import re


class SecretFilter(logging.Filter):
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def redact(self, text: str) -> str:
        if self.token:
            text = text.replace(self.token, "[REDACTED]")
        return re.sub(r"(?:bot)?[1-9][0-9]{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def configure_logging(token: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretFilter(token))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
