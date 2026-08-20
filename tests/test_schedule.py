from datetime import UTC, datetime

import pytest

from digest_bot.schedule import (
    InvalidSchedule,
    digest_date_for,
    digest_date_for_send_time,
    due_delivery,
    validate_send_time,
)


@pytest.mark.parametrize("value", ["00:00", "09:30", "13:59", "20:00", "23:59"])
def test_valid_delivery_times(value: str) -> None:
    assert validate_send_time(value) == value


@pytest.mark.parametrize("value", ["14:00", "17:30", "19:59"])
def test_rejects_quiet_window(value: str) -> None:
    with pytest.raises(InvalidSchedule):
        validate_send_time(value)


def test_morning_uses_yesterday() -> None:
    moment = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    assert digest_date_for(moment).isoformat() == "2026-08-19"


def test_evening_uses_today() -> None:
    moment = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
    assert digest_date_for(moment).isoformat() == "2026-08-20"


def test_quiet_window_has_no_date() -> None:
    assert digest_date_for(datetime(2026, 8, 20, 14, 0, tzinfo=UTC)) is None


def test_due_delivery_catches_up_inside_same_window() -> None:
    now = datetime(2026, 8, 20, 8, 30, tzinfo=UTC)
    due = due_delivery("UTC", "08:00", now)
    assert due is not None
    assert due.digest_date.isoformat() == "2026-08-19"


def test_due_delivery_does_not_cross_quiet_window() -> None:
    now = datetime(2026, 8, 20, 20, 30, tzinfo=UTC)
    assert due_delivery("UTC", "08:00", now) is None


def test_timezone_controls_window_and_date() -> None:
    # 19:30 UTC is 00:30 on the next day in Asia/Yekaterinburg.
    now = datetime(2026, 8, 20, 19, 30, tzinfo=UTC)
    due = due_delivery("Asia/Yekaterinburg", "00:15", now)
    assert due is not None
    assert due.digest_date.isoformat() == "2026-08-20"


def test_preview_date_comes_from_configured_send_window() -> None:
    local_date = datetime(2026, 8, 20, tzinfo=UTC).date()
    assert digest_date_for_send_time(local_date, "09:00").isoformat() == "2026-08-19"
    assert digest_date_for_send_time(local_date, "22:00").isoformat() == "2026-08-20"
