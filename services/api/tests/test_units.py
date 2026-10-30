"""Pure unit tests (no database)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.logging import redact
from app.core.permissions import ROLE_PERMISSIONS, Permission, has_permission
from app.core.phone import InvalidPhoneError, normalize_phone
from app.core.security import SecretBox, hash_token, verify_password
from app.models.calls import Call, CallState
from app.models.campaigns import CampaignLead, CampaignLeadStatus
from app.models.identity import Role
from app.schemas.campaigns import RetryPolicy
from app.services import campaigns as campaign_service
from app.services.leads import _csv_safe
from app.workers.campaigns import _apply_result


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


# ------------------------------------------------------------------ campaign scheduling rules


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_calling_window_across_midnight_is_two_windows() -> None:
    hours = {"mon": [["22:00", "24:00"]], "tue": [["00:00", "02:00"]]}
    tz = ZoneInfo("UTC")
    # 2027-03-01 is a Monday.
    assert campaign_service.within_calling_hours(hours, _at(2027, 3, 1, 23, 30), tz)
    assert campaign_service.within_calling_hours(hours, _at(2027, 3, 2, 1, 59), tz)
    assert not campaign_service.within_calling_hours(hours, _at(2027, 3, 2, 2, 0), tz)
    assert not campaign_service.within_calling_hours(hours, _at(2027, 3, 1, 21, 59), tz)
    # From Tuesday 03:00 the next chance is the following Monday night; inside a window
    # the answer is "now"; without any window there is no answer.
    opens = campaign_service.next_calling_window(hours, _at(2027, 3, 2, 3, 0), tz)
    assert opens == _at(2027, 3, 8, 22, 0)
    now = _at(2027, 3, 1, 23, 30)
    assert campaign_service.next_calling_window(hours, now, tz) == now
    assert campaign_service.next_calling_window({"mon": []}, now, tz) is None
    # A malformed window stored by hand is ignored, not fatal.
    odd = {"mon": [["9am", "5pm"], ["09:00", "17:00"]]}
    assert campaign_service.within_calling_hours(odd, _at(2027, 3, 1, 10, 0), tz)


def test_calling_hours_follow_the_local_clock_through_a_dst_change() -> None:
    hours = {"sat": [["09:00", "17:00"]], "sun": [["09:00", "17:00"]]}
    london = ZoneInfo("Europe/London")
    # British Summer Time starts on Sunday 2027-03-28 at 01:00 UTC.
    assert not campaign_service.within_calling_hours(hours, _at(2027, 3, 27, 8, 30), london)
    assert campaign_service.within_calling_hours(hours, _at(2027, 3, 28, 8, 30), london)
    opens = campaign_service.next_calling_window(hours, _at(2027, 3, 27, 20, 0), london)
    assert opens == _at(2027, 3, 28, 8, 0), "09:00 local, which is now one hour earlier in UTC"
    assert opens is not None and opens.astimezone(london).hour == 9


def test_timezone_falls_back_from_lead_to_campaign_to_organization_to_utc() -> None:
    assert str(campaign_service.resolve_zone(None, "Mars/Olympus", "Asia/Karachi")) == (
        "Asia/Karachi"
    )
    assert str(campaign_service.resolve_zone("", None, None)) == "UTC"
    assert str(campaign_service.resolve_zone("Europe/London", "UTC", None)) == "Europe/London"


def _outcome(state: CallState, attempts: int, policy: RetryPolicy) -> CampaignLead:
    item = CampaignLead(status=CampaignLeadStatus.CALLING, attempts=attempts)
    _apply_result(item, Call(state=state, outcome=None), policy, _at(2027, 3, 1, 0))
    return item


def test_retry_policy_decides_each_result() -> None:
    policy = RetryPolicy(max_attempts=2, retry_on=["no_answer", "busy"], backoff_minutes=30)
    retried = _outcome(CallState.BUSY, 1, policy)
    assert retried.status == CampaignLeadStatus.PENDING and retried.last_outcome == "busy"
    assert retried.next_attempt_at == _at(2027, 3, 1, 0, 30)
    assert _outcome(CallState.NO_ANSWER, 2, policy).status == CampaignLeadStatus.EXHAUSTED
    assert _outcome(CallState.FAILED, 1, policy).status == CampaignLeadStatus.FAILED
    assert _outcome(CallState.CANCELED, 1, policy).status == CampaignLeadStatus.FAILED
    assert _outcome(CallState.COMPLETED, 1, policy).status == CampaignLeadStatus.COMPLETED
    # Failed dials can be opted into; the wait grows with the attempt number.
    lenient = RetryPolicy(max_attempts=3, retry_on=["failed"], backoff_minutes=10)
    second = _outcome(CallState.FAILED, 2, lenient)
    assert second.status == CampaignLeadStatus.PENDING
    assert second.next_attempt_at == _at(2027, 3, 1, 0, 20)
