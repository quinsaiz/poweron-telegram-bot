from typing import TYPE_CHECKING

from src.domain_time import as_kyiv, kyiv_now

if TYPE_CHECKING:
    from datetime import datetime


def format_schedule(times: dict[str, str]) -> str:
    if not times:
        return "⚠️ *Графік відсутній*"

    sorted_times = sorted(times.items())
    status_map = {"0": "🟢 Є світло", "1": "🔴 Немає світла", "10": "🟡 Перемикання"}

    formatted_blocks = []
    current_status = sorted_times[0][1]
    start_time = sorted_times[0][0]

    for i in range(1, len(sorted_times)):
        time, status = sorted_times[i]

        if status != current_status:
            formatted_blocks.append(
                f"`{start_time} — {time}:` {status_map.get(current_status, '⚪️ Невідомо')}"
            )
            start_time = time
            current_status = status

    formatted_blocks.append(
        f"`{start_time} — 24:00:` {status_map.get(current_status, '⚪️ Невідомо')}"
    )

    return "\n".join(formatted_blocks)


def get_current_status(times: dict[str, str], *, now: datetime | None = None) -> str:
    if not times:
        return ""

    now_kyiv = kyiv_now() if now is None else as_kyiv(now, source="Current-status clock")
    current_time = now_kyiv.strftime("%H:%M")

    status_map = {"0": "🟢 Є світло", "1": "🔴 Немає світла", "10": "🟡 Перемикання"}

    sorted_times = sorted(times.items())
    current_status = None

    for i, (time, status) in enumerate(sorted_times):
        if current_time >= time:
            current_status = status
        else:
            break

    if current_status:
        return status_map.get(current_status, "⚪️ Невідомо")

    return ""


def format_date_ua(date: datetime) -> str:
    months_ua = {
        1: "січня",
        2: "лютого",
        3: "березня",
        4: "квітня",
        5: "травня",
        6: "червня",
        7: "липня",
        8: "серпня",
        9: "вересня",
        10: "жовтня",
        11: "листопада",
        12: "грудня",
    }

    day = date.day
    month = months_ua[date.month]

    return f"{day} {month}"
