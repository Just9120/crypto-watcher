import io
import logging

from cryptowatcher.logging_utils import SecretFilter


def test_third_party_message_and_exception_redacted():
    token = "7523865123:test_only_token_not_a_secret"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretFilter(token))
    logger = logging.Logger("isolated-third-party")
    logger.addHandler(handler)
    try:
        raise ValueError("Rejected token " + token)
    except ValueError:
        logger.exception("Request failed at %s", "https://api.telegram.org/bot" + token + "/getMe")
    output = stream.getvalue()
    assert token not in output
    assert "[REDACTED]" in output and "ValueError" in output
