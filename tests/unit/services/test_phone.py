"""Phone normalization must fail as a 400, never a 500 -- and never on a webhook.

`phonenumbers.NumberParseException` is not a subclass of `ValueError` (its bases
are `UnicodeMixin` and `Exception`), so the `except ValueError` blocks wrapped
around every call site never caught it. Unparseable input escaped as an unhandled
500. On the Twilio webhooks that is worse than a bad response: Twilio retries any
non-2xx, so one malformed caller ID became a redelivery storm.
"""

import phonenumbers
import pytest

from src.core.errors import InvalidPhoneNumberError
from src.services.channels import (
    maybe_normalize_phone_number,
    normalize_phone_number,
    try_normalize_phone_number,
)


def test_number_parse_exception_is_not_a_value_error():
    """The assumption the old error handling was built on, stated explicitly."""
    assert not issubclass(phonenumbers.NumberParseException, ValueError)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+14155552671", "+14155552671"),
        ("415-555-2671", "+14155552671"),
        ("(415) 555 2671", "+14155552671"),
    ],
)
def test_normalizes_valid_numbers_to_e164(raw: str, expected: str):
    assert normalize_phone_number(raw, "US") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "not a phone number",  # NumberParseException
        "",  # NumberParseException
        "+",
        "abc123",
        "12",  # parses, but is not a valid number
        "+1555",
    ],
)
def test_invalid_input_raises_a_domain_error(raw: str):
    with pytest.raises(InvalidPhoneNumberError):
        normalize_phone_number(raw, "US")


def test_the_domain_error_maps_to_400_not_500():
    assert InvalidPhoneNumberError().status_code == 400


def test_maybe_variant_passes_none_through_but_still_validates():
    assert maybe_normalize_phone_number(None) is None
    assert maybe_normalize_phone_number("") is None
    with pytest.raises(InvalidPhoneNumberError):
        maybe_normalize_phone_number("garbage", "US")


@pytest.mark.parametrize("raw", ["garbage", "+", "12", None, ""])
def test_try_variant_never_raises(raw):
    """Used by inbound webhooks, where raising would trigger provider retries."""
    assert try_normalize_phone_number(raw, "US") is None


def test_try_variant_still_normalizes_good_input():
    assert try_normalize_phone_number("415-555-2671", "US") == "+14155552671"
