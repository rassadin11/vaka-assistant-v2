"""Shared timezone tables and resolution for onboarding and tools."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

# All eleven Russian time zones, labelled by their offset from Moscow with an anchor city.
TIMEZONE_BUTTONS: list[list[tuple[str, str]]] = [
    [
        ("МСК-1 · Калининград", "tz:Europe/Kaliningrad"),
        ("МСК · Москва, Санкт-Петербург", "tz:Europe/Moscow"),
    ],
    [
        ("МСК+1 · Самара", "tz:Europe/Samara"),
        ("МСК+2 · Екатеринбург", "tz:Asia/Yekaterinburg"),
    ],
    [
        ("МСК+3 · Омск", "tz:Asia/Omsk"),
        ("МСК+4 · Красноярск", "tz:Asia/Krasnoyarsk"),
    ],
    [
        ("МСК+5 · Иркутск", "tz:Asia/Irkutsk"),
        ("МСК+6 · Якутск", "tz:Asia/Yakutsk"),
    ],
    [
        ("МСК+7 · Владивосток", "tz:Asia/Vladivostok"),
        ("МСК+8 · Магадан", "tz:Asia/Magadan"),
    ],
    [("МСК+9 · Камчатка", "tz:Asia/Kamchatka")],
    [("Другой город — напишу текстом", "tz:other")],
]

CITY_TZ: dict[str, str] = {
    # UTC+2 (MSK-1)
    "калининград": "Europe/Kaliningrad",
    # UTC+3 (MSK)
    "москва": "Europe/Moscow",
    "санкт-петербург": "Europe/Moscow",
    "петербург": "Europe/Moscow",
    "нижний новгород": "Europe/Moscow",
    "казань": "Europe/Moscow",
    "ростов-на-дону": "Europe/Moscow",
    "краснодар": "Europe/Moscow",
    "воронеж": "Europe/Moscow",
    "волгоград": "Europe/Volgograd",
    "сочи": "Europe/Moscow",
    "мурманск": "Europe/Moscow",
    "архангельск": "Europe/Moscow",
    "ярославль": "Europe/Moscow",
    "тула": "Europe/Moscow",
    "рязань": "Europe/Moscow",
    "симферополь": "Europe/Simferopol",
    "севастополь": "Europe/Simferopol",
    "минск": "Europe/Minsk",
    # UTC+4 (MSK+1)
    "самара": "Europe/Samara",
    "саратов": "Europe/Saratov",
    "тольятти": "Europe/Samara",
    "ижевск": "Europe/Samara",
    "ульяновск": "Europe/Ulyanovsk",
    "астрахань": "Europe/Astrakhan",
    # UTC+5 (MSK+2)
    "екатеринбург": "Asia/Yekaterinburg",
    "челябинск": "Asia/Yekaterinburg",
    "уфа": "Asia/Yekaterinburg",
    "пермь": "Asia/Yekaterinburg",
    "тюмень": "Asia/Yekaterinburg",
    "сургут": "Asia/Yekaterinburg",
    "оренбург": "Asia/Yekaterinburg",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "ташкент": "Asia/Tashkent",
    # UTC+6 (MSK+3)
    "омск": "Asia/Omsk",
    "бишкек": "Asia/Bishkek",
    # UTC+7 (MSK+4)
    "новосибирск": "Asia/Novosibirsk",
    "красноярск": "Asia/Krasnoyarsk",
    "барнаул": "Asia/Barnaul",
    "томск": "Asia/Tomsk",
    "кемерово": "Asia/Novokuznetsk",
    "новокузнецк": "Asia/Novokuznetsk",
    # UTC+8 (MSK+5)
    "иркутск": "Asia/Irkutsk",
    "улан-удэ": "Asia/Irkutsk",
    "братск": "Asia/Irkutsk",
    # UTC+9 (MSK+6)
    "якутск": "Asia/Yakutsk",
    "чита": "Asia/Chita",
    "благовещенск": "Asia/Yakutsk",
    # UTC+10 (MSK+7)
    "владивосток": "Asia/Vladivostok",
    "хабаровск": "Asia/Vladivostok",
    # UTC+11 (MSK+8)
    "магадан": "Asia/Magadan",
    "южно-сахалинск": "Asia/Sakhalin",
    # UTC+12 (MSK+9)
    "камчатка": "Asia/Kamchatka",
    "петропавловск-камчатский": "Asia/Kamchatka",
    "анадырь": "Asia/Anadyr",
    # Other CIS capitals
    "ереван": "Asia/Yerevan",
    "тбилиси": "Asia/Tbilisi",
    "баку": "Asia/Baku",
    "киев": "Europe/Kyiv",
    "кишинёв": "Europe/Chisinau",
}

VALID_TIMEZONES = frozenset(available_timezones())


def resolve_timezone(city: str) -> str | None:
    """Resolve a user-supplied city or IANA identifier to a valid timezone."""

    raw = city.strip()
    if raw == "":
        return None
    known = CITY_TZ.get(re.sub(r"\s+", " ", raw).lower())
    if known is not None:
        return known
    return raw if raw in VALID_TIMEZONES else None


def local_time_fields(timezone: str, current: datetime | None = None) -> dict[str, str]:
    """Return the local wall clock and weekday for a timezone."""

    zone = ZoneInfo(timezone)
    local = current.astimezone(zone) if current is not None else datetime.now(zone)
    return {"local_time": f"{local:%Y-%m-%d %H:%M}", "weekday": f"{local:%A}".lower()}
