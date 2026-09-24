from datetime import UTC, datetime
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kyiv")


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_kyiv(value: datetime, *, source: str = "Clock") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{source} must return an aware datetime")
    return value.astimezone(KYIV_TZ)


def kyiv_now() -> datetime:
    return as_kyiv(utc_now(), source="UTC clock")
