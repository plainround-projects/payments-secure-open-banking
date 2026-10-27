import io
import logging
import random

import pytest

from open_banking.application.logs import fields
from open_banking.infrastructure.logging import configure_logging, redact
from open_banking.infrastructure.retry import retry


@pytest.mark.parametrize(
    "text", ["4242424242424242", "4242 4242 4242 4242", "card 4242-4242-4242-4242 declined", "order 12345678901234567"]
)
def test_anything_shaped_like_a_card_number_is_masked(text):
    assert "4242" not in redact(text) and "[redacted-number]" in redact(text)


def test_ordinary_numbers_are_left_alone():
    assert redact("amount 4250 id 1234567 ts 2026-10-20T09:00:00") == "amount 4250 id 1234567 ts 2026-10-20T09:00:00"


def test_log_lines_are_logfmt_with_fields_and_withhold_secrets():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("t").info(
        "captured", extra=fields(payment_id="p1", card_token="tok_secret", note="a b", pan="4242424242424242")
    )
    line = stream.getvalue().strip()
    assert line.startswith("ts=") and "level=INFO" in line and "msg=captured" in line
    assert "payment_id=p1" in line and 'note="a b"' in line
    assert "card_token=[withheld]" in line and "tok_secret" not in line
    assert "4242424242424242" not in line


def test_exceptions_are_logged_on_one_line():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")
    assert len(stream.getvalue().strip().splitlines()) == 1 and "ValueError" in stream.getvalue()


def test_retry_returns_the_first_success_without_sleeping():
    sleeps: list[float] = []
    assert retry(lambda: 7, attempts=3, base_delay_seconds=1, retry_on=(OSError,), sleep=sleeps.append) == 7
    assert sleeps == []


def test_retry_backs_off_exponentially_with_jitter_and_stops_at_the_last_attempt():
    calls: list[int] = []
    sleeps: list[float] = []
    notes: list[int] = []

    def flaky():
        calls.append(1)
        raise OSError("down")

    with pytest.raises(OSError, match="down"):
        retry(
            flaky,
            attempts=4,
            base_delay_seconds=1,
            retry_on=(OSError,),
            sleep=sleeps.append,
            rng=random.Random(1),
            on_retry=lambda attempt, error: notes.append(attempt),
        )
    assert len(calls) == 4 and notes == [1, 2, 3]
    assert [0.5 <= pause / (2**index) <= 1.0 for index, pause in enumerate(sleeps)] == [True, True, True]


def test_retry_does_not_catch_other_errors():
    with pytest.raises(KeyError):
        retry(lambda: {"a": 1}["x"], attempts=3, base_delay_seconds=0, retry_on=(OSError,))


def test_retry_needs_at_least_one_attempt():
    with pytest.raises(ValueError, match="attempts"):
        retry(lambda: 1, attempts=0, base_delay_seconds=0, retry_on=(OSError,))
