"""Unit and Integration tests for Persian Jalali Calendar, Notes, Daily Notes, Tasks, and Reminders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.jalali import (
    PERSIAN_MONTHS,
    format_full_jalali,
    format_jalali_date,
    get_current_jalali,
    get_days_in_jalali_month,
    get_jalali_month_first_weekday,
    gregorian_to_jalali,
    is_jalali_leap_year,
    jalali_to_datetime,
    jalali_to_gregorian,
    parse_jalali_string,
)
from app.db.memory import InMemoryRepository
from app.main import create_app

PASSWORD = "test-password-1234"


@pytest.fixture
async def client(settings: Settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def _register_user(client: AsyncClient, email: str = "caluser@example.com") -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "تقویم کاربر"},
    )
    assert resp.status_code == 201, resp.text
    tokens = resp.json()["tokens"]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ==============================================================================
# 1. JALALI ENGINE UNIT TESTS
# ==============================================================================

def test_jalali_conversions_roundtrip():
    # Test Nowruz 1405: 2026-03-21 -> 1405/01/01
    jy, jm, jd = gregorian_to_jalali(2026, 3, 21)
    assert (jy, jm, jd) == (1405, 1, 1)

    gy, gm, gd = jalali_to_gregorian(1405, 1, 1)
    assert (gy, gm, gd) == (2026, 3, 21)

    # Roundtrip across various dates
    test_dates = [
        (2023, 1, 1),
        (2024, 2, 29),  # Gregorian leap day
        (2025, 9, 15),
        (2026, 9, 17),
        (2030, 12, 31),
    ]
    for gy, gm, gd in test_dates:
        jy, jm, jd = gregorian_to_jalali(gy, gm, gd)
        back_gy, back_gm, back_gd = jalali_to_gregorian(jy, jm, jd)
        assert (back_gy, back_gm, back_gd) == (gy, gm, gd)


def test_jalali_leap_and_month_days():
    # 1399 was leap, 1403 was leap
    assert is_jalali_leap_year(1399) is True
    assert is_jalali_leap_year(1403) is True
    assert is_jalali_leap_year(1404) is False

    # Months 1-6 have 31 days
    for m in range(1, 7):
        assert get_days_in_jalali_month(1405, m) == 31

    # Months 7-11 have 30 days
    for m in range(7, 12):
        assert get_days_in_jalali_month(1405, m) == 30

    # Month 12: 30 if leap, 29 if not
    assert get_days_in_jalali_month(1403, 12) == 30
    assert get_days_in_jalali_month(1404, 12) == 29


def test_jalali_first_weekday_and_formatting():
    # 1405/01/01 is 2026-03-21, which is Saturday -> weekday 0 in Shamsi order (شنبه)
    first_w = get_jalali_month_first_weekday(1405, 1)
    assert first_w == 0

    assert format_jalali_date(1405, 6, 27) == "1405/06/27"
    assert parse_jalali_string("1405/06/27") == (1405, 6, 27)
    assert parse_jalali_string("1405-06-27") == (1405, 6, 27)
    assert parse_jalali_string("invalid-date") is None

    full_text = format_full_jalali()
    assert any(month in full_text for month in PERSIAN_MONTHS)


# ==============================================================================
# 2. IN-MEMORY REPOSITORY TESTS (NOTES & REMINDERS)
# ==============================================================================

@pytest.mark.asyncio
async def test_repo_notes_crud(repo: InMemoryRepository):
    user = await repo.create_user(email="n@test.com", password_hash="x", display_name="User")
    uid = user["id"]

    # Create general note
    note = await repo.create_note(
        owner_id=uid,
        title="ایده پروژه",
        content="ساخت سیستم یادداشت درایو",
        date_shamsi="1405/06/27",
        is_daily=False,
        color="#38bdf8",
    )
    assert note["id"] is not None
    assert note["title"] == "ایده پروژه"

    # Create daily note
    daily = await repo.create_note(
        owner_id=uid,
        title="یادداشت روزانه",
        content="کارهای امروز...",
        date_shamsi="1405/06/27",
        is_daily=True,
    )
    assert daily["is_daily"] is True

    # List notes with filters
    all_notes = await repo.list_notes(owner_id=uid)
    assert len(all_notes) == 2

    daily_only = await repo.list_notes(owner_id=uid, is_daily=True)
    assert len(daily_only) == 1
    assert daily_only[0]["id"] == daily["id"]

    # Update note
    updated = await repo.update_note(note["id"], owner_id=uid, title="ایده جدید")
    assert updated["title"] == "ایده جدید"

    # Delete note
    ok = await repo.delete_note(note["id"], owner_id=uid)
    assert ok is True
    remaining = await repo.list_notes(owner_id=uid)
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_repo_reminders_and_dispatcher(repo: InMemoryRepository):
    user = await repo.create_user(email="r@test.com", password_hash="x", display_name="User")
    uid = user["id"]

    past_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    future_time = datetime.now(timezone.utc) + timedelta(hours=2)

    # Create due reminder
    due_rem = await repo.create_reminder(
        owner_id=uid,
        title="تماس فوری",
        type="task",
        remind_at_utc=past_time,
        telegram_chat_id=123456789,
    )

    # Create future reminder
    future_rem = await repo.create_reminder(
        owner_id=uid,
        title="جلسه فردا",
        type="occasion",
        remind_at_utc=future_time,
        telegram_chat_id=123456789,
    )

    # Check due reminders
    due_list = await repo.list_due_reminders()
    assert any(r["id"] == due_rem["id"] for r in due_list)
    assert not any(r["id"] == future_rem["id"] for r in due_list)

    # Mark notified
    ok = await repo.mark_reminder_notified(due_rem["id"])
    assert ok is True
    due_list_after = await repo.list_due_reminders()
    assert not any(r["id"] == due_rem["id"] for r in due_list_after)

    # Toggle completed
    toggled = await repo.toggle_reminder(due_rem["id"], owner_id=uid)
    assert toggled["is_completed"] is True

    # Snooze
    snoozed = await repo.snooze_reminder(due_rem["id"], minutes=15)
    assert snoozed["remind_at_utc"] is not None
    assert snoozed["is_notified"] is False


# ==============================================================================
# 3. HTTP API TESTS FOR NOTES, REMINDERS, AND CALENDAR
# ==============================================================================

@pytest.mark.asyncio
async def test_notes_api_endpoints(client: AsyncClient):
    headers = await _register_user(client, "notes_api@test.com")

    # 1. Create note
    create_resp = await client.post(
        "/api/v1/notes",
        headers=headers,
        json={
            "title": "خرید سرور",
            "content": "کانفیگ داکر و ریلوِی",
            "date_shamsi": "1405/06/27",
            "is_daily": False,
            "color": "#10b981",
        },
    )
    assert create_resp.status_code == 201
    note_data = create_resp.json()
    note_id = note_data["id"]

    # 2. Get note
    get_resp = await client.get(f"/api/v1/notes/{note_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["title"] == "خرید سرور"

    # 3. List notes
    list_resp = await client.get("/api/v1/notes", headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 4. Update note
    patch_resp = await client.patch(
        f"/api/v1/notes/{note_id}",
        headers=headers,
        json={"title": "خرید سرور جدید"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["title"] == "خرید سرور جدید"

    # 5. Delete note
    del_resp = await client.delete(f"/api/v1/notes/{note_id}", headers=headers)
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_calendar_and_reminders_api_endpoints(client: AsyncClient):
    headers = await _register_user(client, "cal_api@test.com")

    # 1. Get today calendar
    today_resp = await client.get("/api/v1/calendar/today", headers=headers)
    assert today_resp.status_code == 200
    today_data = today_resp.json()
    assert "date_shamsi" in today_data
    assert "full_text" in today_data
    assert today_data["year"] > 1400

    # 2. Create task reminder
    task_resp = await client.post(
        "/api/v1/reminders",
        headers=headers,
        json={
            "title": "ورزش صبحگاهی",
            "type": "task",
            "date_shamsi": today_data["date_shamsi"],
            "time_str": "07:30",
        },
    )
    assert task_resp.status_code == 201
    task_data = task_resp.json()
    task_id = task_data["id"]

    # 3. Create countdown timer
    timer_resp = await client.post(
        "/api/v1/reminders",
        headers=headers,
        json={
            "title": "دم کردن چای",
            "type": "timer",
            "timer_minutes": 10,
        },
    )
    assert timer_resp.status_code == 201
    assert timer_resp.json()["type"] == "timer"

    # 4. List reminders
    list_resp = await client.get("/api/v1/reminders", headers=headers)
    assert list_resp.status_code == 200
    assert len(list_resp.json()) >= 2

    # 5. Toggle task completion
    toggle_resp = await client.post(f"/api/v1/reminders/{task_id}/toggle", headers=headers)
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["is_completed"] is True

    # 6. Snooze reminder
    snooze_resp = await client.post(f"/api/v1/reminders/{task_id}/snooze?minutes=20", headers=headers)
    assert snooze_resp.status_code == 200

    # 7. Get monthly calendar data
    month_resp = await client.get(
        f"/api/v1/calendar/month?year={today_data['year']}&month={today_data['month']}",
        headers=headers,
    )
    assert month_resp.status_code == 200
    m_data = month_resp.json()
    assert m_data["year"] == today_data["year"]
    assert m_data["days_in_month"] in (29, 30, 31)
    assert "reminders" in m_data
    assert len(m_data["reminders"]) >= 1

    # 8. Delete reminder
    del_resp = await client.delete(f"/api/v1/reminders/{task_id}", headers=headers)
    assert del_resp.status_code == 204
