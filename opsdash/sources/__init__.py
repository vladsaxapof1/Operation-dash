"""Источники данных. Какие источники собираются по расписанию — config.SOURCES; здесь — чем их собирать и проверять."""

from __future__ import annotations

from typing import Callable


def collector(key: str) -> Callable[[], str]:
    """Сборщик источника config.SOURCES[key]: возвращает строку для журнала («новых писем: 3»)."""
    from opsdash.sources import calendar, kaiten, mail, pachca, telegram, ymessenger

    return {
        "telegram": telegram.collect, "ymessenger": ymessenger.collect, "pachca": pachca.collect,
        "mail": mail.collect, "gmail": mail.collect_gmail, "imap": mail.collect_imap,
        "calendar": calendar.collect, "google_calendar": calendar.google_collect,
        "caldav": calendar.caldav_collect, "ics_calendar": calendar.ics_collect, "kaiten": kaiten.collect,
    }[key]


def checker(key: str) -> Callable[[], str]:
    """Проверка подключения: «Проверить подключение» в окне источника и ./ops check."""
    from opsdash.sources import calendar, kaiten, mail, pachca, telegram, ymessenger

    return {
        "telegram": telegram.check, "ymessenger": ymessenger.check, "pachca": pachca.check,
        "mail": mail.check, "gmail": mail.check_gmail, "imap": mail.check_imap,
        "calendar": calendar.check, "google_calendar": calendar.google_check,
        "caldav": calendar.caldav_check, "ics_calendar": calendar.ics_check, "kaiten": kaiten.check,
    }[key]
