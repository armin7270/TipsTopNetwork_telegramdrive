"""API routes for reminders, tasks, occasions, timers, and Persian calendar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.core.jalali import (
    PERSIAN_MONTHS,
    format_full_jalali,
    format_jalali_date,
    get_current_jalali,
    get_days_in_jalali_month,
    get_jalali_month_first_weekday,
    jalali_to_datetime,
    parse_jalali_string,
)
from app.services.auth import Principal, current_user

router = APIRouter(prefix="/reminders", tags=["reminders"])


class ReminderCreateRequest(BaseModel):
    title: str = Field(default="وظیفه جدید", max_length=255)
    type: str = Field(default="task")  # task, occasion, timer
    date_shamsi: str | None = Field(default=None, max_length=20)
    time_str: str | None = Field(default=None, max_length=10)  # e.g. "14:30"
    timer_minutes: int | None = Field(default=None)  # for countdown timers
    repeat: str = Field(default="none")


@router.get("", summary="List reminders, tasks, and occasions")
async def list_reminders(
    request: Request,
    principal: Principal = Depends(current_user),
    date_shamsi: str | None = Query(default=None),
    type: str | None = Query(default=None),
    include_completed: bool = Query(default=True),
) -> list[dict[str, Any]]:
    repo = request.app.state.repo
    return await repo.list_reminders(
        owner_id=principal.user_id,
        date_shamsi=date_shamsi,
        type=type,
        include_completed=include_completed,
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a reminder, task, or occasion")
async def create_reminder(
    payload: ReminderCreateRequest,
    request: Request,
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    repo = request.app.state.repo
    user = await repo.get_user(principal.user_id)
    telegram_chat_id = user.get("telegram_user_id") if user else None

    # Determine remind_at_utc
    remind_at_utc = None
    date_shamsi = payload.date_shamsi
    time_str = payload.time_str

    if payload.timer_minutes and payload.timer_minutes > 0:
        remind_at_utc = datetime.now(timezone.utc) + timedelta(minutes=payload.timer_minutes)
        jy, jm, jd = get_current_jalali()
        date_shamsi = format_jalali_date(jy, jm, jd)
        now_utc = datetime.now(timezone.utc) + timedelta(hours=3.5)
        time_str = f"{now_utc.hour:02d}:{now_utc.minute:02d}"
    elif date_shamsi:
        parsed = parse_jalali_string(date_shamsi)
        if parsed:
            jy, jm, jd = parsed
            hour = 9
            minute = 0
            if time_str and ":" in time_str:
                try:
                    parts = time_str.split(":")
                    hour = int(parts[0])
                    minute = int(parts[1])
                except Exception:
                    pass
            remind_at_utc = jalali_to_datetime(jy, jm, jd, hour=hour, minute=minute)

    return await repo.create_reminder(
        owner_id=principal.user_id,
        title=payload.title,
        type=payload.type,
        date_shamsi=date_shamsi,
        time_str=time_str,
        remind_at_utc=remind_at_utc,
        repeat=payload.repeat,
        telegram_chat_id=telegram_chat_id,
    )


@router.post("/{reminder_id}/toggle", summary="Toggle reminder completion status")
async def toggle_reminder(
    reminder_id: str,
    request: Request,
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    repo = request.app.state.repo
    updated = await repo.toggle_reminder(reminder_id, owner_id=principal.user_id)
    if not updated:
        raise HTTPException(status_code=404, detail="یادآور یافت نشد")
    return updated


@router.post("/{reminder_id}/snooze", summary="Snooze reminder by N minutes")
async def snooze_reminder(
    reminder_id: str,
    request: Request,
    minutes: int = Query(default=10),
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    repo = request.app.state.repo
    updated = await repo.snooze_reminder(reminder_id, minutes=minutes)
    if not updated:
        raise HTTPException(status_code=404, detail="یادآور یافت نشد")
    return updated


@router.delete("/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a reminder")
async def delete_reminder(
    reminder_id: str,
    request: Request,
    principal: Principal = Depends(current_user),
) -> None:
    repo = request.app.state.repo
    ok = await repo.delete_reminder(reminder_id, owner_id=principal.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="یادآور یافت نشد")


calendar_router = APIRouter(prefix="/calendar", tags=["calendar"])


@calendar_router.get("/today", summary="Get today's Shamsi calendar information")
async def get_today_calendar(
    request: Request,
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    repo = request.app.state.repo
    jy, jm, jd = get_current_jalali()
    today_shamsi = format_jalali_date(jy, jm, jd)
    full_text = format_full_jalali()

    notes = await repo.list_notes(owner_id=principal.user_id, date_shamsi=today_shamsi)
    reminders = await repo.list_reminders(owner_id=principal.user_id, date_shamsi=today_shamsi)

    return {
        "date_shamsi": today_shamsi,
        "year": jy,
        "month": jm,
        "day": jd,
        "full_text": full_text,
        "daily_notes": notes,
        "reminders": reminders,
    }


@calendar_router.get("/month", summary="Get monthly calendar data with days, notes, and reminders")
async def get_month_calendar(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    principal: Principal = Depends(current_user),
) -> dict[str, Any]:
    repo = request.app.state.repo
    cur_y, cur_m, cur_d = get_current_jalali()
    jy = year or cur_y
    jm = month or cur_m

    if jm < 1:
        jm = 12
        jy -= 1
    elif jm > 12:
        jm = 1
        jy += 1

    days_in_month = get_days_in_jalali_month(jy, jm)
    first_weekday = get_jalali_month_first_weekday(jy, jm)
    month_name = PERSIAN_MONTHS[jm - 1]
    today_shamsi = format_jalali_date(cur_y, cur_m, cur_d)

    month_prefix = f"{jy:04d}/{jm:02d}/"
    all_notes = await repo.list_notes(owner_id=principal.user_id)
    month_notes = [n for n in all_notes if (n.get("date_shamsi") or "").startswith(month_prefix)]

    all_reminders = await repo.list_reminders(owner_id=principal.user_id)
    month_reminders = [r for r in all_reminders if (r.get("date_shamsi") or "").startswith(month_prefix)]

    return {
        "year": jy,
        "month": jm,
        "month_name": month_name,
        "days_in_month": days_in_month,
        "first_day_weekday": first_weekday,
        "today_shamsi": today_shamsi,
        "notes": month_notes,
        "reminders": month_reminders,
    }
