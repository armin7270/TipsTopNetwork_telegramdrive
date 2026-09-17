"""High-precision, zero-dependency Jalali (Solar Hijri / تقویم شمسی) calendar utility.

Implements bidirectional conversion between Gregorian and Jalali calendars,
Persian date formatting, weekday translations, and relative date parsing.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Tuple


PERSIAN_MONTHS = [
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
]

PERSIAN_WEEKDAYS = [
    "دوشنبه",
    "سه‌شنبه",
    "چهارشنبه",
    "پنج‌شنبه",
    "جمعه",
    "شنبه",
    "یکشنبه",
]

PERSIAN_WEEKDAYS_SHAMSI_ORDER = [
    "شنبه",
    "یکشنبه",
    "دوشنبه",
    "سه‌شنبه",
    "چهارشنبه",
    "پنج‌شنبه",
    "جمعه",
]


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> Tuple[int, int, int]:
    """Convert Gregorian (year, month, day) to Jalali (year, month, day)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621

    gy2 = gy if gm > 2 else gy - 1
    days = (
        365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        - 80
        + gd
        + g_d_m[gm - 1]
    )
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461

    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365

    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30

    return jy, jm, jd


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> Tuple[int, int, int]:
    """Convert Jalali (year, month, day) to Gregorian (year, month, day)."""
    if jy > 979:
        gy = 1600
        jy -= 979
    else:
        gy = 621
        jy -= 0

    days = (
        365 * jy
        + (jy // 33) * 8
        + ((jy % 33) + 3) // 4
        + 78
        + jd
        + ((jm - 1) * 31 if jm < 7 else ((jm - 7) * 30) + 186)
    )
    gy += 400 * (days // 146097)
    days %= 146097

    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1

    gy += 4 * (days // 1461)
    days %= 1461

    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365

    gd = days + 1
    sal_a = [
        0,
        31,
        29
        if ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0)
        else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    gm = 0
    while gm < 13 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1

    return gy, gm, gd


def is_jalali_leap_year(jy: int) -> bool:
    """Check whether a Jalali year is leap (366 days)."""
    gy, gm, gd = jalali_to_gregorian(jy, 12, 30)
    back_y, back_m, back_d = gregorian_to_jalali(gy, gm, gd)
    return back_y == jy and back_m == 12 and back_d == 30


def get_current_jalali(tz_offset_hours: float = 3.5) -> Tuple[int, int, int]:
    """Get current date in Jalali calendar (default Iran Standard Time +03:30)."""
    utc_now = datetime.now(timezone.utc)
    local_now = utc_now + timedelta(hours=tz_offset_hours)
    return gregorian_to_jalali(local_now.year, local_now.month, local_now.day)


def get_days_in_jalali_month(jy: int, jm: int) -> int:
    """Return the number of days in a given Jalali year and month."""
    if 1 <= jm <= 6:
        return 31
    elif 7 <= jm <= 11:
        return 30
    elif jm == 12:
        return 30 if is_jalali_leap_year(jy) else 29
    return 30


def get_jalali_month_first_weekday(jy: int, jm: int) -> int:
    """Return weekday index for the 1st day of the Jalali month (0=Saturday, 6=Friday)."""
    gy, gm, gd = jalali_to_gregorian(jy, jm, 1)
    d = date(gy, gm, gd)
    return (d.weekday() + 2) % 7


def format_jalali_date(jy: int, jm: int, jd: int) -> str:
    """Format as standard YYYY/MM/DD."""
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def get_jalali_weekday_name(gy: int, gm: int, gd: int) -> str:
    """Return Persian weekday name for a Gregorian date."""
    d = date(gy, gm, gd)
    # d.weekday(): Monday is 0, Sunday is 6
    return PERSIAN_WEEKDAYS[d.weekday()]


def format_full_jalali(dt: datetime | None = None, tz_offset_hours: float = 3.5) -> str:
    """Format full Persian date string e.g. 'پنج‌شنبه ۲۶ شهریور ۱۴۰۵'."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        local_dt = dt + timedelta(hours=tz_offset_hours)
    else:
        local_dt = dt + timedelta(hours=tz_offset_hours)

    jy, jm, jd = gregorian_to_jalali(local_dt.year, local_dt.month, local_dt.day)
    wname = get_jalali_weekday_name(local_dt.year, local_dt.month, local_dt.day)
    mname = PERSIAN_MONTHS[jm - 1]
    return f"{wname} {jd} {mname} {jy}"


def parse_jalali_string(date_str: str) -> Tuple[int, int, int] | None:
    """Parse '1405/06/26' or '1405-06-26' into (jy, jm, jd)."""
    cleaned = date_str.strip().replace("-", "/").replace(".", "/")
    parts = cleaned.split("/")
    if len(parts) == 3:
        try:
            jy, jm, jd = int(parts[0]), int(parts[1]), int(parts[2])
            if 1300 <= jy <= 1500 and 1 <= jm <= 12 and 1 <= jd <= 31:
                return jy, jm, jd
        except ValueError:
            return None
    return None


def jalali_to_datetime(
    jy: int, jm: int, jd: int, hour: int = 0, minute: int = 0, tz_offset_hours: float = 3.5
) -> datetime:
    """Convert Jalali date + local Iran time to UTC datetime."""
    gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
    local_dt = datetime(gy, gm, gd, hour, minute, tzinfo=timezone.utc)
    # Convert from Iran local to UTC
    utc_dt = local_dt - timedelta(hours=tz_offset_hours)
    return utc_dt
