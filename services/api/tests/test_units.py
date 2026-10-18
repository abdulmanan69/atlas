"""Pure unit tests (no database)."""

from __future__ import annotations

import itertools

import pytest

from app.core.logging import redact
from app.core.permissions import ROLE_PERMISSIONS, Permission, has_permission
from app.core.phone import InvalidPhoneError, normalize_phone
from app.core.security import SecretBox, hash_token, verify_password
from app.models.identity import Role
from app.services.leads import _csv_safe


@pytest.mark.parametrize(
    ("raw", "region", "expected"),
    [
        ("+44 20 7946 0000", "US", "+442079460000"),
        ("020 7946 0000", "GB", "+442079460000"),
        ("0300 1234567", "PK", "+923001234567"),
    ],
)
def test_normalize_phone(raw: str, region: str, expected: str) -> None:
    assert normalize_phone(raw, region) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "123", "+1 000"])
def test_normalize_phone_rejects(raw: str) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw, "US")


def test_roles_are_strictly_nested() -> None:
    order = [Role.VIEWER, Role.AGENT, Role.MANAGER, Role.ADMIN, Role.OWNER]
    for lower, higher in itertools.pairwise(order):
        assert ROLE_PERMISSIONS[lower] < ROLE_PERMISSIONS[higher]
    assert not has_permission(Role.ADMIN, Permission.ORG_MANAGE)
    assert not has_permission(Role.MANAGER, Permission.CAMPAIGNS_LAUNCH_LIVE)


def test_redact_masks_nested_secrets() -> None:
    out = redact(
        {"api_key": "x", "nested": {"Authorization": "Bearer abc"}, "note": "Bearer abc.def"}
    )
    assert out == {
        "api_key": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]"},
        "note": "Bearer [REDACTED]",
    }


def test_csv_safe() -> None:
    assert _csv_safe("=1+1") == "'=1+1"
    assert _csv_safe("-5") == "'-5"
    assert _csv_safe("Normal") == "Normal"
    assert _csv_safe(None) == ""


def test_secret_box_roundtrip_and_tamper() -> None:
    from cryptography.fernet import Fernet

    box = SecretBox(Fernet.generate_key().decode())
    token = box.encrypt("sk-test-123")
    assert token != "sk-test-123" and box.decrypt(token) == "sk-test-123"
    with pytest.raises(RuntimeError):
        SecretBox(Fernet.generate_key().decode()).decrypt(token)


def test_verify_password_handles_missing_hash() -> None:
    assert verify_password("anything", None) is False
    assert len(hash_token("t")) == 64


def test_display_phone() -> None:
    from app.core.phone import display_phone

    assert display_phone("+442079460001") == "+44 20 7946 0001"
    assert display_phone("+14155550100") == "+1 415-555-0100"
    assert display_phone(None) is None
