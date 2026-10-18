"""Phone number normalization to E.164."""

from __future__ import annotations

import phonenumbers


class InvalidPhoneError(ValueError):
    pass


def normalize_phone(raw: str, default_region: str = "US") -> str:
    """Return E.164 form or raise InvalidPhoneError.

    A number that parses is not necessarily callable; consent and DNC checks
    are separate and mandatory before any call.
    """
    candidate = raw.strip()
    if not candidate:
        raise InvalidPhoneError("Phone number is empty")
    try:
        parsed = phonenumbers.parse(candidate, default_region.upper())
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneError("Phone number could not be parsed") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneError("Phone number is not valid")
    return str(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))


def display_phone(e164: str | None) -> str | None:
    """International human-readable form, e.g. '+44 20 7946 0001'."""
    if not e164:
        return None
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return e164
    return str(phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL))
