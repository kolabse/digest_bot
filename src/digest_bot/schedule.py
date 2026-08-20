from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MORNING_END = time(14, 0)
EVENING_START = time(20, 0)


class InvalidSchedule(ValueError):
    pass


def parse_send_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidSchedule("Укажите время в формате ЧЧ:ММ, например 09:30.") from exc
    if parsed.second or parsed.microsecond:
        raise InvalidSchedule("Время должно быть указано с точностью до минуты.")
    return parsed


def is_delivery_window(value: time) -> bool:
    return value < MORNING_END or value >= EVENING_START


def validate_send_time(value: str) -> str:
    parsed = parse_send_time(value)
    if not is_delivery_window(parsed):
        raise InvalidSchedule(
            "Рассылка доступна с 00:00 до 13:59 и с 20:00 до 23:59. "
            "С 14:00 до 20:00 сообщения не отправляются."
        )
    return parsed.strftime("%H:%M")


def validate_timezone(value: str) -> str:
    candidate = value.strip()
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise InvalidSchedule(
            "Неизвестный часовой пояс. Используйте имя IANA, например Europe/Moscow."
        ) from exc
    return candidate


def digest_date_for(moment: datetime) -> date | None:
    local_time = moment.timetz().replace(tzinfo=None)
    if local_time < MORNING_END:
        return moment.date() - timedelta(days=1)
    if local_time >= EVENING_START:
        return moment.date()
    return None


def digest_date_for_send_time(local_date: date, send_time: str) -> date:
    scheduled = parse_send_time(send_time)
    if not is_delivery_window(scheduled):
        raise InvalidSchedule("Настроенное время находится вне окна рассылки.")
    if scheduled < MORNING_END:
        return local_date - timedelta(days=1)
    return local_date


@dataclass(frozen=True, slots=True)
class DueDelivery:
    digest_date: date
    local_now: datetime


def due_delivery(timezone: str, send_time: str, now: datetime) -> DueDelivery | None:
    local_now = now.astimezone(ZoneInfo(timezone))
    digest_date = digest_date_for(local_now)
    if digest_date is None:
        return None
    scheduled = parse_send_time(send_time)
    current = local_now.timetz().replace(tzinfo=None)
    same_window = (scheduled < MORNING_END and current < MORNING_END) or (
        scheduled >= EVENING_START and current >= EVENING_START
    )
    if not same_window or current < scheduled:
        return None
    return DueDelivery(digest_date=digest_date, local_now=local_now)
