"""Full-featured Telegram Bot Service for TeleDrive.

Features:
- Handles /start, /files, /usage, /search, /help commands.
- Interactive inline keyboards for navigating files, direct downloads, and deletions.
- Direct file uploads: users send or forward ANY media/document to the bot, which
  automatically chunks, encrypts, and registers the file in TeleDrive.
- Real-time upload notifications: sends an alert to the user and/or storage channel
  whenever a file is uploaded via the website, android app, or bot.
- WebApp integration: allows opening the Liquid Glass web interface right inside Telegram.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.jalali import (
    format_full_jalali,
    format_jalali_date,
    get_current_jalali,
    jalali_to_datetime,
    parse_jalali_string,
)

log = logging.getLogger(__name__)


def format_bytes(size: int | float) -> str:
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = min(int(math.log(size, 1024)), len(units) - 1)
    s = round(size / (1024**i), 2)
    return f"{s} {units[i]}"


class TelegramBotService:
    """Production-ready asynchronous Telegram Bot and Notification engine."""

    def __init__(
        self,
        settings: Any,
        repo: Any,
        pool: Any = None,
        upload_service: Any = None,
        download_service: Any = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.pool = pool
        self.upload_service = upload_service
        self.download_service = download_service

        self.bot_token: str | None = getattr(settings, "telegram_bot_token", None) or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.channel_id: str | None = (
            str(settings.storage_pool_channel_ids[0])
            if getattr(settings, "storage_pool_channel_ids", None)
            else os.environ.get("STORAGE_POOL_CHANNEL_IDS")
        )

        self._task: asyncio.Task[None] | None = None
        self._reminder_task: asyncio.Task[None] | None = None
        self._running = False
        self._bot_info: dict[str, Any] | None = None
        self._client: httpx.AsyncClient | None = None
        self._user_state: dict[int, dict[str, Any]] = {}

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    @property
    def public_base_url(self) -> str:
        """Derive canonical public base URL for links and WebApp.
        
        Prioritizes environment variables, Railway public domain, and falls back
        to the production URL, ensuring HTTPS protocol.
        """
        for var in ("PUBLIC_URL", "APP_URL", "SERVER_URL", "TELEDRIVE_PUBLIC_URL"):
            val = os.environ.get(var, "").strip()
            if val:
                if not val.startswith(("http://", "https://")):
                    val = f"https://{val}"
                return val.rstrip("/")

        railway_domain = (
            os.environ.get("RAILWAY_PUBLIC_DOMAIN")
            or os.environ.get("RAILWAY_STATIC_URL")
            or ""
        ).strip()
        if railway_domain:
            if not railway_domain.startswith(("http://", "https://")):
                railway_domain = f"https://{railway_domain}"
            return railway_domain.rstrip("/")

        server_host = os.environ.get("SERVER_HOST", "").strip()
        if server_host and not server_host.startswith("127.0.0.1") and not server_host.startswith("localhost"):
            if not server_host.startswith(("http://", "https://")):
                server_host = f"https://{server_host}"
            return server_host.rstrip("/")

        return "https://tipstopnetworktelegramdrive-production.up.railway.app"

    def _mint_download_token(self, user: dict[str, Any] | None) -> str | None:
        """Mint a 7-day signed JWT access token for direct browser downloads."""
        if not user:
            return None
        try:
            from app.services.auth import AuthService
            auth_service = AuthService(repository=self.repo, settings=self.settings)
            return auth_service.create_download_token(user)
        except Exception as e:
            log.warning("Could not mint download token: %s", e)
            return None

    async def start(self) -> None:
        """Start the bot polling loop and register commands."""
        if getattr(self.settings, "use_fake_telegram", False):
            log.info("TelegramBotService: running in virtual/mock mode; real Telegram Bot polling disabled.")
            return

        if not self.bot_token:
            log.info("TelegramBotService: TELEGRAM_BOT_TOKEN is empty; bot will not poll.")
            return

        self._running = True
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))

        try:
            # Delete any existing webhook to ensure getUpdates polling works cleanly (avoids 409 Conflict)
            try:
                await self._client.post(
                    f"{self.api_base}/deleteWebhook",
                    json={"drop_pending_updates": False},
                )
            except Exception as e:
                log.debug("deleteWebhook note: %s", e)

            resp = await self._client.get(f"{self.api_base}/getMe")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    self._bot_info = data["result"]
                    username = self._bot_info.get("username", "Unknown")
                    log.info("Telegram Bot @%s successfully connected!", username)

                    # Set bot commands in Telegram UI
                    commands = [
                        {"command": "start", "description": "منوی اصلی و ورود"},
                        {"command": "files", "description": "مدیریت و مشاهده فایل‌های من"},
                        {"command": "notes", "description": "یادداشت‌ها و یادداشت روزانه"},
                        {"command": "remind", "description": "تنظیم یادآور، مناسبت و تایمر"},
                        {"command": "calendar", "description": "تقویم شمسی و رویدادهای امروز"},
                        {"command": "usage", "description": "سهمیه باقی‌مانده و میزان مصرف"},
                        {"command": "search", "description": "جستجوی فایل در درایو"},
                        {"command": "link", "description": "اتصال به حساب وب درایو"},
                        {"command": "help", "description": "راهنمای کار با ربات"},
                    ]
                    await self._client.post(
                        f"{self.api_base}/setMyCommands",
                        json={"commands": commands},
                    )
                else:
                    log.warning("Telegram Bot API returned not ok: %s", data)
            else:
                log.warning("Failed to connect Telegram Bot: %s", resp.text)
        except Exception as e:
            log.warning("TelegramBotService start check failed: %s", e)

        # Launch polling loop and reminder dispatcher in background
        self._task = asyncio.create_task(self._poll_loop())
        self._reminder_task = asyncio.create_task(self._reminder_dispatcher_loop())

    async def stop(self) -> None:
        """Stop polling loop and close HTTP client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._reminder_task and not self._reminder_task.done():
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        log.info("TelegramBotService stopped.")

    async def restart(self, bot_token: str | None = None, channel_id: str | None = None) -> None:
        """Dynamically update credentials and restart bot service."""
        if bot_token:
            self.bot_token = bot_token
        if channel_id:
            self.channel_id = channel_id
        await self.stop()
        if self.bot_token:
            await self.start()

    # --- Notification System --------------------------------------------------

    async def notify_upload(self, node: dict[str, Any], principal: Any) -> None:
        """Send a notification when an upload finishes on Web, Android, or Bot."""
        if not self.bot_token or not self._client:
            return

        name = node.get("name", "Unnamed file")
        size_str = format_bytes(node.get("size_bytes", 0))
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Determine user info
        user_name = "کاربر درایو"
        telegram_id = None
        user = None
        try:
            user_id = getattr(principal, "user_id", str(principal))
            user = await self.repo.get_user(user_id)
            if user:
                user_name = user.get("display_name") or user.get("email") or "کاربر"
                telegram_id = user.get("telegram_user_id")
        except Exception:
            pass

        if not telegram_id and hasattr(self.repo, "users"):
            for u in self.repo.users.values():
                if u.get("telegram_user_id"):
                    telegram_id = u["telegram_user_id"]
                    break

        token = self._mint_download_token(user)
        token_param = f"?token={token}" if token else ""
        download_url = f"{self.public_base_url}/api/v1/files/{node['id']}/content{token_param}"
        web_url = f"{self.public_base_url}/"

        text = (
            "🔔 *آپلود جدید در درایو ابری TeleDrive*\n\n"
            f"📄 *نام فایل:* `{name}`\n"
            f"💾 *حجم فایل:* `{size_str}`\n"
            f"👤 *کاربر:* `{user_name}`\n"
            f"🕒 *زمان:* `{now_str}`\n\n"
            f"🔗 [دانلود مستقیم و پخش آنلاین]({download_url})"
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "⬇️ دریافت فایل", "url": download_url},
                    {"text": "🌐 ورود به درایو", "url": web_url},
                ]
            ]
        }

        # 1. Send to user's Telegram chat if linked
        if telegram_id:
            try:
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={
                        "chat_id": telegram_id,
                        "text": f"✅ *فایل شما با موفقیت ذخیره شد!*\n\n{text}",
                        "parse_mode": "Markdown",
                        "reply_markup": reply_markup,
                    },
                )
            except Exception as e:
                log.debug("Failed sending upload notice to user %s: %s", telegram_id, e)

        # 2. Send to storage/admin channel if configured
        if self.channel_id:
            try:
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={
                        "chat_id": self.channel_id,
                        "text": text,
                        "parse_mode": "Markdown",
                        "reply_markup": reply_markup,
                    },
                )
            except Exception as e:
                log.debug("Failed sending upload notice to channel %s: %s", self.channel_id, e)

    # --- Polling and Dispatching ----------------------------------------------

    async def _poll_loop(self) -> None:
        """Long-polling for bot events."""
        offset = 0
        conflict_count = 0
        while self._running:
            try:
                if not self._client:
                    await asyncio.sleep(2)
                    continue

                resp = await self._client.get(
                    f"{self.api_base}/getUpdates",
                    params={"offset": offset, "timeout": 20},
                    timeout=30.0,
                )
                if resp.status_code == 409:
                    conflict_count += 1
                    if conflict_count <= 2:
                        log.warning("getUpdates 409 Conflict: webhook or another instance active, clearing webhook...")
                        try:
                            await self._client.post(
                                f"{self.api_base}/deleteWebhook",
                                json={"drop_pending_updates": False},
                            )
                        except Exception:
                            pass
                        await asyncio.sleep(3)
                    else:
                        log.warning(
                            "getUpdates 409 Conflict persists (another bot instance is actively polling). "
                            "Backing off polling for 45s to avoid spamming Telegram..."
                        )
                        await asyncio.sleep(45)
                    continue

                conflict_count = 0
                if resp.status_code != 200:
                    await asyncio.sleep(3)
                    continue

                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(3)
                    continue

                for update in data.get("result", []):
                    offset = max(offset, update["update_id"] + 1)
                    try:
                        await self._handle_update(update)
                    except Exception as exc:
                        log.exception("Error handling update %s: %s", update.get("update_id"), exc)

            except (httpx.RequestError, asyncio.TimeoutError):
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Poll loop error: %s", e)
                await asyncio.sleep(2)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """Route updates to messages or callback queries."""
        if "message" in update:
            await self._handle_message(update["message"])
        elif "callback_query" in update:
            await self._handle_callback(update["callback_query"])

    # --- Message Handler ------------------------------------------------------

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        from_user = message.get("from", {})
        user_id = from_user.get("id")
        text = (message.get("text") or message.get("caption") or "").strip()

        # Check for media/documents first (direct file upload)
        if any(k in message for k in ("document", "video", "audio", "photo", "voice")):
            await self._handle_incoming_file(message)
            return

        if not text:
            return

        # Check if user is in an interactive state
        state = self._user_state.get(user_id)
        if state:
            action = state.get("action")
            if action == "rename":
                await self._handle_rename_input(chat_id, user_id, text, state)
                return
            elif action in ("add_note", "add_daily", "add_reminder", "add_timer"):
                await self._handle_interactive_text_input(chat_id, user_id, text, state)
                return

        if text.startswith("/start"):
            await self._send_welcome(chat_id, from_user)
        elif text.startswith("/files") or text in ("📂 فایل‌های من", "فایل‌های من", "فایل های من"):
            await self._send_file_list(chat_id, user_id, page=0)
        elif text.startswith("/notes") or text in ("📝 یادداشت‌ها", "یادداشت‌ها", "یادداشت ها"):
            await self._send_notes_list(chat_id, user_id, page=0)
        elif text.startswith("/daily") or text in ("📅 یادداشت امروز", "یادداشت امروز"):
            await self._send_today_note(chat_id, user_id)
        elif text.startswith("/calendar") or text in ("📅 تقویم شمسی", "تقویم", "تقویم شمسی"):
            await self._send_calendar(chat_id, user_id)
        elif text.startswith("/remind") or text.startswith("/tasks") or text in ("⏰ یادآورها و تایمر", "یادآورها", "تسک‌ها", "مناسبت‌ها"):
            await self._handle_remind_command(chat_id, user_id, text)
        elif text.startswith("/usage") or text.startswith("/stats") or text in ("📊 وضعیت سهمیه", "📊 وضعیت فضای ابری", "سهمیه"):
            await self._send_usage_stats(chat_id, user_id)
        elif text.startswith("/link"):
            await self._handle_link_account(chat_id, user_id, text)
        elif text.startswith("/search"):
            query = text.replace("/search", "").strip()
            await self._send_search(chat_id, user_id, query)
        elif text.startswith("/help") or text in ("ℹ️ راهنما", "راهنما"):
            await self._send_help(chat_id)
        else:
            # If user sends plain text, search for matching files
            await self._send_search(chat_id, user_id, text)

    async def _handle_rename_input(
        self, chat_id: int, user_id: int, text: str, state: dict[str, Any]
    ) -> None:
        new_name = text.strip()
        if new_name in ("/cancel", "انصراف", "لغو"):
            self._user_state.pop(user_id, None)
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": "❌ عملیات ویرایش نام فایل لغو شد."},
            )
            await self._send_file_list(chat_id, user_id, page=0)
            return

        node_id = state.get("node_id")
        old_name = state.get("old_name", "")
        raw_node = getattr(self.repo, "nodes", {}).get(node_id)
        if not raw_node:
            self._user_state.pop(user_id, None)
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": "❌ فایل مورد نظر دیگر در درایو یافت نشد."},
            )
            return

        # Preserve file extension if user omitted it
        if "." in old_name and "." not in new_name:
            ext = old_name.rsplit(".", 1)[1]
            new_name = f"{new_name}.{ext}"

        try:
            owner_id = raw_node.get("owner_id")
            await self.repo.rename_node(node_id, owner_id, new_name)
            self._user_state.pop(user_id, None)

            user = await self._get_or_create_user(user_id)
            token = self._mint_download_token(user)
            token_param = f"?token={token}" if token else ""
            download_url = f"{self.public_base_url}/api/v1/files/{node_id}/content{token_param}"
            hex_id = str(node_id).replace("-", "")

            action_row = []
            size_int = node.get("size_bytes", 0) if node else 0
            if size_int <= 50 * 1024 * 1024:
                action_row.append({"text": "📥 دریافت در تلگرام", "callback_data": f"get_{hex_id}"})
            action_row.append({"text": "🌐 دانلود با نام جدید", "url": download_url})

            keyboard = {
                "inline_keyboard": [
                    action_row,
                    [
                        {"text": "✏️ ویرایش مجدد", "callback_data": f"ren_{hex_id}"},
                        {"text": "🗑️ حذف فایل", "callback_data": f"askdel_{hex_id}"},
                    ],
                    [{"text": "📂 فایل‌های من", "callback_data": "files_0"}],
                ]
            }
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        f"✅ *نام فایل با موفقیت تغییر یافت!*\n\n"
                        f"📄 نام قبلی: `{old_name}`\n"
                        f"✨ نام جدید: `{new_name}`\n\n"
                        f"تغییرات به طور لحظه‌ای در وب‌اپلیکیشن و ربات ذخیره شد."
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        except Exception as e:
            log.exception("Rename error: %s", e)
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"❌ خطا در ویرایش نام فایل: `{str(e)}`",
                    "parse_mode": "Markdown",
                },
            )

    async def _handle_link_account(self, chat_id: int, user_id: int, text: str) -> None:
        target = text.replace("/link", "").strip()
        if not target:
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        "🔗 *اتصال حساب وب به ربات تلگرام:*\n\n"
                        "جهت دسترسی به فایل‌های آپلودشده از طریق وب‌سایت در ربات، دستور زیر را با ایمیل یا نام کاربری خود در وب ارسال کنید:\n"
                        "`/link youremail@example.com`"
                    ),
                    "parse_mode": "Markdown",
                },
            )
            return

        target_lower = target.lower()
        found_user = None
        for u in getattr(self.repo, "users", {}).values():
            if (
                (u.get("email") and u["email"].lower() == target_lower)
                or (u.get("display_name") and u["display_name"].lower() == target_lower)
                or (str(u.get("id")) == target)
            ):
                found_user = u
                break

        if found_user:
            for u in getattr(self.repo, "users", {}).values():
                if u.get("telegram_user_id") == user_id and u["id"] != found_user["id"]:
                    u["telegram_user_id"] = None
            found_user["telegram_user_id"] = user_id
            if hasattr(self.repo, "_save_state"):
                self.repo._save_state()
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        f"🎉 *اتصال حساب کاربری موفقیت‌آمیز بود!*\n\n"
                        f"اکانت شما با ایمیل `{found_user.get('email') or found_user.get('display_name')}` به ربات متصل شد.\n"
                        "تمام فایل‌ها و سهمیه شما هم‌اکنون به صورت همگام در دسترس است."
                    ),
                    "parse_mode": "Markdown",
                },
            )
            await self._send_file_list(chat_id, user_id, page=0)
        else:
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"❌ کاربری با مشخصات «{target}» در سیستم یافت نشد.\nلطفاً ایمیل ثبت‌نامی خود در سایت را بررسی فرمایید.",
                    "parse_mode": "Markdown",
                },
            )

    # --- Commands -------------------------------------------------------------

    async def _send_welcome(self, chat_id: int, from_user: dict[str, Any]) -> None:
        name = from_user.get("first_name", "کاربر عزیز")
        web_url = f"{self.public_base_url}/"
        today_fa = format_full_jalali()

        welcome_text = (
            f"👋 سلام *{name}*!\n\n"
            f"📅 امروز: *{today_fa}*\n"
            "☁️ *به درایو ابری و دستیار هوشمند TeleDrive خوش آمدید!*\n\n"
            "امکانات در دسترس شما:\n"
            "• آپلود نامحدود و امن فایل‌ها بر بستر تلگرام\n"
            "• استریم آنلاین ویدیو و آهنگ + لینک دانلود مستقیم\n"
            "• مدیریت کامل فایل‌ها (دانلود در تلگرام، تغییر نام و حذف)\n"
            "• 📝 **ثبت یادداشت‌های روزانه و عمومی**\n"
            "• 📅 **تقویم شمسی و رویدادهای روز**\n"
            "• ⏰ **تنظیم تایمر، هشدار وظایف و مناسبت‌های خاص با اعلان در تلگرام**\n\n"
            "👇 یکی از گزینه‌های زیر را انتخاب کنید یا دستور دلخواه را ارسال نمایید:"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📂 فایل‌های من", "callback_data": "files_0"},
                    {"text": "📊 سهمیه باقی‌مانده", "callback_data": "usage"},
                ],
                [
                    {"text": "📝 یادداشت‌ها", "callback_data": "notes_0"},
                    {"text": "📅 تقویم شمسی", "callback_data": "cal_today"},
                ],
                [
                    {"text": "⏰ یادآورها و تایمر", "callback_data": "reminders_menu"},
                    {"text": "🔍 جستجوی فایل", "callback_data": "search_prompt"},
                ],
                [
                    {"text": "ℹ️ راهنما و امکانات", "callback_data": "help"},
                    {"text": "🌐 ورود به وب‌درایو", "url": web_url},
                ],
            ]
        }

        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": welcome_text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
        )

    async def _send_help(self, chat_id: int) -> None:
        help_text = (
            "📖 *راهنمای کامل امکانات و دستورات ربات TeleDrive*\n\n"
            "📤 *آپلود مستقیم:* کافیست هرگونه فایل، عکس، فیلم، آهنگ یا سند را همینجا برای ربات بفرستید یا فوروارد کنید.\n\n"
            "📂 *فایل‌های من (`/files`):* مشاهده لیست کامل فایل‌های ذخیره‌شده، دریافت لینک مستقیم، تغییر نام و حذف فایل.\n\n"
            "✏️ *ویرایش و تغییر نام فایل:* با زدن دکمه «ویرایش نام» زیر هر فایل می‌توانید بدون نیاز به دانلود، نام فایل را به دلخواه عوض کنید.\n\n"
            "🗑️ *حذف فایل:* با دکمه «حذف فایل» می‌توانید فایل‌های غیرضروری را حذف کنید تا حجم سهمیه شما آزاد گردد.\n\n"
            "📊 *سهمیه باقی‌مانده (`/usage`):* مشاهده دقیق حجم مصرف‌شده، حجم آزاد باقی‌مانده و درصد مصرف همراه با نوار وضعیت گرافیکی.\n\n"
            "🔍 *جستجو (`/search <نام>`):* جستجوی سریع بین تمامی فایل‌های ذخیره‌شده.\n\n"
            "🔗 *اتصال اکانت وب (`/link <ایمیل>`):* همگام‌سازی حساب کاربری وب با ربات تلگرام.\n\n"
            "🛡️ *امنیت:* تمام چانک‌های فایل با الگوریتم نظامی AES-256-GCM رمزنگاری شده و بر بستر امن تلگرام میزبانی می‌شوند."
        )
        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={"chat_id": chat_id, "text": help_text, "parse_mode": "Markdown"},
        )

    async def _send_usage_stats(self, chat_id: int, tg_user_id: int) -> None:
        user = await self._get_or_create_user(tg_user_id)
        owner_id = user["id"]

        # recompute_usage returns an integer (bytes used)
        used_bytes = await self.repo.recompute_usage(owner_id)
        quota_bytes = user.get("quota_bytes") or (100 * 1024**3)
        remaining_bytes = max(0, quota_bytes - used_bytes)

        percent = min(100.0, (used_bytes / quota_bytes * 100)) if quota_bytes > 0 else 0.0
        bar_len = 10
        filled = min(bar_len, int(round((percent / 100.0) * bar_len)))
        bar = "🟩" * filled + "⬜" * (bar_len - filled)

        breakdown = await self.repo.usage_breakdown(owner_id) if hasattr(self.repo, "usage_breakdown") else {}
        files_count = breakdown.get("file_count")
        if files_count is None:
            files_count = len([
                n for n in getattr(self.repo, "nodes", {}).values()
                if n.get("owner_id") == owner_id and n.get("kind") == "file" and not n.get("trashed_at")
            ])
        folders_count = breakdown.get("folder_count", 0)

        used_str = format_bytes(used_bytes)
        quota_str = format_bytes(quota_bytes)
        remaining_str = format_bytes(remaining_bytes)

        msg = (
            "📊 *وضعیت مصرف فضای ابری TeleDrive*\n\n"
            f"👤 *کاربر:* `{user.get('display_name') or user.get('email') or 'شما'}`\n"
            f"💾 *فضای مصرف‌شده:* `{used_str}`\n"
            f"🟢 *سهمیه باقی‌مانده:* `{remaining_str}`\n"
            f"📦 *کل فضای اختصاصی:* `{quota_str}`\n"
            f"📈 *میزان اشغال فضا:* `{percent:.1f}%`\n"
            f"{bar}\n\n"
            f"📁 *تعداد کل فایل‌ها:* `{files_count}` عدد\n"
            f"📂 *تعداد پوشه‌ها:* `{folders_count}` عدد\n"
            "🛡️ *امنیت:* رمزنگاری نظامی فعال (AES-256-GCM)\n"
            "⚡ *وضعیت سرور:* آنلاین و متصل"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "📂 فایل‌های من", "callback_data": "files_0"}],
                [{"text": "🌐 ورود به وب‌درایو", "url": f"{self.public_base_url}/"}],
                [{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "start"}],
            ]
        }
        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            },
        )

    async def _send_file_list(self, chat_id: int, tg_user_id: int, page: int = 0) -> None:
        user = await self._get_or_create_user(tg_user_id)
        owner_id = user["id"]

        # Collect all active file nodes owned by user across the entire drive
        file_nodes = [
            n for n in getattr(self.repo, "nodes", {}).values()
            if str(n.get("owner_id")) == str(owner_id)
            and n.get("kind") == "file"
            and not n.get("trashed_at")
        ]

        # Fail-safe 1: If user has no files, check if files exist under any account in this drive
        if not file_nodes and hasattr(self.repo, "nodes"):
            for candidate_node in self.repo.nodes.values():
                if candidate_node.get("kind") == "file" and not candidate_node.get("trashed_at"):
                    cand_owner_id = str(candidate_node.get("owner_id"))
                    cand_user = None
                    if hasattr(self.repo, "users"):
                        for u_id, u_obj in self.repo.users.items():
                            if str(u_id) == cand_owner_id:
                                cand_user = u_obj
                                break
                    if cand_user:
                        cand_user["telegram_user_id"] = tg_user_id
                        user["telegram_user_id"] = None
                        user = cand_user
                        owner_id = cand_owner_id
                        if hasattr(self.repo, "_save_state"):
                            self.repo._save_state()
                        file_nodes = [
                            n for n in self.repo.nodes.values()
                            if str(n.get("owner_id")) == str(owner_id)
                            and n.get("kind") == "file"
                            and not n.get("trashed_at")
                        ]
                        break

        # Fail-safe 2: In a personal single-tenant drive, if still empty, show all active files
        if not file_nodes and hasattr(self.repo, "nodes"):
            file_nodes = [
                n for n in self.repo.nodes.values()
                if n.get("kind") == "file" and not n.get("trashed_at")
            ]

        file_nodes.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

        if not file_nodes:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "➕ هر فایلی را بفرستید تا ذخیره شود", "callback_data": "noop"}],
                    [{"text": "🌐 ورود به وب‌درایو", "url": f"{self.public_base_url}/"}],
                    [{"text": "🔙 بازگشت به منو", "callback_data": "start"}],
                ]
            }
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        "📭 *درایو شما در حال حاضر خالی است.*\n\n"
                        "کافیست هرگونه عکس، ویدیو، موزیک یا سندی را در همین صفحه برای ربات بفرستید یا فوروارد کنید تا ذخیره شود!"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
            return

        PAGE_SIZE = 5
        total_pages = max(1, math.ceil(len(file_nodes) / PAGE_SIZE))
        page = max(0, min(page, total_pages - 1))
        page_items = file_nodes[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

        text = (
            f"📂 *مدیریت فایل‌های شما در TeleDrive*\n"
            f"📊 صفحه {page + 1} از {total_pages} (کل فایل‌ها: {len(file_nodes)} عدد)\n\n"
        )
        keyboard_rows = []

        token = self._mint_download_token(user)
        token_param = f"?token={token}" if token else ""

        for idx, node in enumerate(page_items, start=page * PAGE_SIZE + 1):
            name = node.get("name", "file")
            size_bytes = int(node.get("size_bytes", 0))
            size = format_bytes(size_bytes)
            emoji = self._get_emoji(name)
            node_id = str(node["id"])
            hex_id = node_id.replace("-", "")

            text += f"{idx}. {emoji} *{name}*\n   💾 حجم: `{size}`\n\n"

            download_url = f"{self.public_base_url}/api/v1/files/{node_id}/content{token_param}"

            row_actions = []
            if size_bytes <= 50 * 1024 * 1024:
                row_actions.append({"text": "📥 دریافت در تلگرام", "callback_data": f"get_{hex_id}"})
            row_actions.append({"text": "🌐 لینک دانلود مستقیم", "url": download_url})
            keyboard_rows.append(row_actions)

            keyboard_rows.append([
                {"text": "✏️ ویرایش نام", "callback_data": f"ren_{hex_id}"},
                {"text": "🗑️ حذف فایل", "callback_data": f"askdel_{hex_id}"},
            ])

        # Pagination navigation
        nav_row = []
        if page > 0:
            nav_row.append({"text": "⬅️ قبلی", "callback_data": f"files_{page - 1}"})
        if page < total_pages - 1:
            nav_row.append({"text": "بعدی ➡️", "callback_data": f"files_{page + 1}"})
        if nav_row:
            keyboard_rows.append(nav_row)

        keyboard_rows.append([
            {"text": "📊 سهمیه باقی‌مانده", "callback_data": "usage"},
            {"text": "🔙 بازگشت به منوی اصلی", "callback_data": "start"},
        ])

        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": keyboard_rows},
            },
        )

    async def _send_search(self, chat_id: int, tg_user_id: int, query: str) -> None:
        if not query:
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "🔍 برای جستجو، لطفاً نام فایل را بنویسید:\nمثال: `/search video.mp4`",
                    "parse_mode": "Markdown",
                },
            )
            return

        user = await self._get_or_create_user(tg_user_id)
        owner_id = user["id"]
        root = await self.repo.get_root_node(owner_id)

        all_nodes = await self.repo.list_children(owner_id=owner_id, parent_id=root["id"]) if root else []
        matched = [n for n in all_nodes if query.lower() in n.get("name", "").lower()]

        if not matched:
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": f"🔍 هیچ فایلی با نام «{query}» پیدا نشد."},
            )
            return

        token = self._mint_download_token(user)
        token_param = f"?token={token}" if token else ""
        text = f"🔍 *نتایج جستجو برای «{query}»:* ({len(matched)} مورد)\n\n"
        buttons = []

        for node in matched[:10]:
            name = node.get("name", "file")
            size = format_bytes(node.get("size_bytes", 0))
            emoji = self._get_emoji(name)
            text += f"• {emoji} *{name}* ({size})\n"
            url = f"{self.public_base_url}/api/v1/files/{node['id']}/content{token_param}"
            buttons.append([{"text": f"⬇️ دریافت {name[:24]}", "url": url}])

        buttons.append([{"text": "🔙 بازگشت", "callback_data": "files_0"}])

        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": buttons},
            },
        )

    # --- Direct File Upload from Telegram -------------------------------------

    async def _handle_incoming_file(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        from_user = message.get("from", {})
        tg_user_id = from_user.get("id")

        # Extract file info
        file_obj = None
        file_name = None
        mime_type = None

        if "document" in message:
            file_obj = message["document"]
            file_name = file_obj.get("file_name")
            mime_type = file_obj.get("mime_type")
        elif "video" in message:
            file_obj = message["video"]
            file_name = file_obj.get("file_name") or f"video_{file_obj['file_unique_id']}.mp4"
            mime_type = file_obj.get("mime_type") or "video/mp4"
        elif "audio" in message:
            file_obj = message["audio"]
            file_name = file_obj.get("file_name") or f"audio_{file_obj['file_unique_id']}.mp3"
            mime_type = file_obj.get("mime_type") or "audio/mpeg"
        elif "voice" in message:
            file_obj = message["voice"]
            file_name = f"voice_{file_obj['file_unique_id']}.ogg"
            mime_type = "audio/ogg"
        elif "photo" in message:
            file_obj = message["photo"][-1]  # Highest quality
            file_name = f"photo_{file_obj['file_unique_id']}.jpg"
            mime_type = "image/jpeg"

        if not file_obj:
            return

        file_size = file_obj.get("file_size", 0)
        file_id = file_obj.get("file_id")

        # Telegram Bot API limit for direct getFile download is 20 MB
        BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024

        if file_size > BOT_API_DOWNLOAD_LIMIT:
            msg = (
                f"⚠️ *فایل انتخابی ({format_bytes(file_size)}) بزرگتر از محدودیت ربات (۲۰ مگابایت) است.*\n\n"
                "برای آپلود فوق‌سریع فایل‌های پرحجم و گیگابایتی بدون هیچ محدودیتی، "
                "می‌توانید از **وب‌اپلیکیشن شیشه‌ای TeleDrive** یا اپلیکیشن اندروید استفاده نمایید:"
            )
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🌐 ورود به وب‌اپلیکیشن TeleDrive", "url": f"{self.public_base_url}/"}],
                    [{"text": "🔙 بازگشت به منو", "callback_data": "start"}],
                ]
            }
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "reply_markup": keyboard},
            )
            return

        # Send initial progress message
        wait_resp = await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"⏳ *در حال دریافت «{file_name}» و ذخیره‌سازی ابری در TeleDrive...*",
                "parse_mode": "Markdown",
            },
        )
        wait_msg_id = wait_resp.json().get("result", {}).get("message_id")

        try:
            # 1. Fetch file path from Telegram
            file_info_resp = await self._client.get(
                f"{self.api_base}/getFile",
                params={"file_id": file_id},
            )
            file_info = file_info_resp.json()
            if not file_info.get("ok"):
                raise RuntimeError(file_info.get("description", "getFile failed"))

            file_path = file_info["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"

            # 2. Download file content
            content_resp = await self._client.get(download_url)
            content_resp.raise_for_status()
            content_bytes = content_resp.content

            # 3. Store into TeleDrive Virtual File System (VFS)
            user = await self._get_or_create_user(tg_user_id)
            owner_id = user["id"]
            root = await self.repo.get_root_node(owner_id)
            root_id = root["id"] if root else None

            # Calculate whole-file SHA-256
            hasher = hashlib.sha256(content_bytes)
            file_sha256 = hasher.hexdigest()

            node_id = None
            if self.upload_service:
                try:
                    session = await self.upload_service.create_session(
                        owner_id=owner_id,
                        parent_id=root_id,
                        name=file_name,
                        size_bytes=len(content_bytes),
                        mime_type=mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream",
                        encryption_mode="server_managed",
                    )
                    node_id = session["node_id"]
                    upload_id = session["id"]
                    await self.upload_service.put_chunk(
                        upload_id=upload_id,
                        owner_id=owner_id,
                        chunk_index=0,
                        body=content_bytes,
                        declared_sha256=file_sha256,
                    )
                    await self.upload_service.finish_upload(
                        upload_id=upload_id,
                        owner_id=owner_id,
                        declared_sha256=file_sha256,
                    )
                except Exception as upload_err:
                    log.warning("upload_service in bot failed, using repo fallback: %s", upload_err)
                    node_id = None

            if not node_id:
                node = await self.repo.create_file_node(
                    owner_id=owner_id,
                    parent_id=root_id,
                    name=file_name,
                    size_bytes=len(content_bytes),
                    mime_type=mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream",
                    chunk_size=1024 * 1024,
                    total_chunks=1,
                    encryption_mode="server_managed",
                    upload_state="uploading",
                )
                node_id = node["id"]
                pools = await self.repo.list_storage_pools() if hasattr(self.repo, "list_storage_pools") else []
                pool_id = pools[0]["id"] if pools else "pool-default"
                channel_id_num = (
                    int(self.channel_id)
                    if (self.channel_id and str(self.channel_id).lstrip("-").isdigit())
                    else -1001111111111
                )
                await self.repo.insert_chunk(
                    node_id=node_id,
                    chunk_index=0,
                    plaintext_size=len(content_bytes),
                    ciphertext_size=len(content_bytes),
                    sha256=bytes.fromhex(file_sha256),
                    iv=b"\x00" * 12,
                    auth_tag=b"\x00" * 16,
                    storage_pool_id=pool_id,
                    telegram_channel_id=channel_id_num,
                    telegram_message_id=message.get("message_id", 1),
                    telegram_file_id=file_id,
                )
                await self.repo.finalize_file_node(
                    node_id=node_id,
                    owner_id=owner_id,
                    sha256_hex=file_sha256,
                    hash_mode="plaintext_sha256",
                )
                await self.repo.recompute_usage(owner_id)
                if hasattr(self.repo, "_save_state"):
                    self.repo._save_state()

            # Update confirmation message
            token = self._mint_download_token(user)
            token_param = f"?token={token}" if token else ""
            direct_link = f"{self.public_base_url}/api/v1/files/{node_id}/content{token_param}"
            hex_id = str(node_id).replace("-", "")

            done_text = (
                "🎉 *فایل با موفقیت در TeleDrive ذخیره شد!*\n\n"
                f"📄 *نام فایل:* `{file_name}`\n"
                f"💾 *حجم:* `{format_bytes(len(content_bytes))}`\n"
                f"🕒 *زمان:* `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                f"🔗 [دانلود مستقیم و پخش آنلاین]({direct_link})"
            )

            action_row = []
            if len(content_bytes) <= 50 * 1024 * 1024:
                action_row.append({"text": "📥 دریافت در تلگرام", "callback_data": f"get_{hex_id}"})
            action_row.append({"text": "🌐 لینک دانلود مستقیم", "url": direct_link})

            keyboard = {
                "inline_keyboard": [
                    action_row,
                    [
                        {"text": "✏️ ویرایش نام", "callback_data": f"ren_{hex_id}"},
                        {"text": "🗑️ حذف فایل", "callback_data": f"askdel_{hex_id}"},
                    ],
                    [
                        {"text": "📂 فایل‌های من", "callback_data": "files_0"},
                        {"text": "📊 سهمیه باقی‌مانده", "callback_data": "usage"},
                    ],
                ]
            }

            if wait_msg_id:
                await self._client.post(
                    f"{self.api_base}/editMessageText",
                    json={
                        "chat_id": chat_id,
                        "message_id": wait_msg_id,
                        "text": done_text,
                        "parse_mode": "Markdown",
                        "reply_markup": keyboard,
                    },
                )

        except Exception as e:
            log.exception("Upload from Telegram failed: %s", e)
            err_msg = f"❌ *خطا در ذخیره‌سازی فایل:* `{str(e)}`"
            if wait_msg_id:
                await self._client.post(
                    f"{self.api_base}/editMessageText",
                    json={"chat_id": chat_id, "message_id": wait_msg_id, "text": err_msg, "parse_mode": "Markdown"},
                )

    # --- Callbacks ------------------------------------------------------------

    async def _handle_callback(self, cb: dict[str, Any]) -> None:
        cb_id = cb["id"]
        data = cb.get("data", "")
        message = cb.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        user_id = cb.get("from", {}).get("id")

        await self._client.post(f"{self.api_base}/answerCallbackQuery", json={"callback_query_id": cb_id})

        if not chat_id:
            return

        if data == "start":
            await self._send_welcome(chat_id, cb.get("from", {}))
        elif data.startswith("files_"):
            page = int(data.split("_")[1])
            await self._send_file_list(chat_id, user_id, page=page)
        elif data == "usage":
            await self._send_usage_stats(chat_id, user_id)
        elif data == "help":
            await self._send_help(chat_id)
        elif data == "search_prompt":
            await self._send_search(chat_id, user_id, "")
        elif data.startswith("get_"):
            hex_id = data.split("_")[1]
            node = self._find_node_by_hex(hex_id)
            if not node:
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": "❌ فایل مورد نظر یافت نشد."},
                )
                return
            await self._send_file_to_chat(chat_id, user_id, node)
        elif data.startswith("askdel_"):
            hex_id = data.split("_")[1]
            node = self._find_node_by_hex(hex_id)
            if not node:
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": "❌ فایل مورد نظر یافت نشد."},
                )
                return
            name = node.get("name", "فایل")
            size = format_bytes(node.get("size_bytes", 0))
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🗑️ بله، فایل حذف شود", "callback_data": f"dodel_{hex_id}"},
                        {"text": "❌ انصراف", "callback_data": "files_0"},
                    ]
                ]
            }
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        f"⚠️ *تأیید حذف دائمی فایل:*\n\n"
                        f"📄 نام فایل: `{name}`\n"
                        f"💾 حجم: `{size}`\n\n"
                        "آیا از حذف دائمی این فایل از درایو ابری تلگرام مطمئن هستید؟"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        elif data.startswith("dodel_") or data.startswith("del_"):
            hex_id = data.split("_")[1]
            node = self._find_node_by_hex(hex_id)
            if not node:
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": "❌ فایل مورد نظر یافت نشد."},
                )
                return
            user = await self._get_or_create_user(user_id)
            owner_id = node.get("owner_id", user["id"])
            await self.repo.delete_node(node["id"], owner_id)
            await self.repo.recompute_usage(owner_id)
            if hasattr(self.repo, "_save_state"):
                self.repo._save_state()
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"🗑️ فایل *«{node.get('name')}»* با موفقیت حذف گردید و سهمیه شما آزاد شد.",
                    "parse_mode": "Markdown",
                },
            )
            await self._send_file_list(chat_id, user_id, page=0)
        elif data.startswith("ren_"):
            hex_id = data.split("_")[1]
            node = self._find_node_by_hex(hex_id)
            if not node:
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": "❌ فایل مورد نظر یافت نشد."},
                )
                return
            name = node.get("name", "فایل")
            self._user_state[user_id] = {
                "action": "rename",
                "node_id": node["id"],
                "old_name": name,
            }
            keyboard = {
                "inline_keyboard": [
                    [{"text": "❌ انصراف از ویرایش", "callback_data": "cancel_rename"}]
                ]
            }
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        f"✏️ *ویرایش و تغییر نام فایل:*\n\n"
                        f"📄 نام فعلی فایل: `{name}`\n\n"
                        "لطفاً **نام جدید فایل** را در پیام بعدی تایپ و ارسال کنید:"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        elif data == "cancel_rename":
            self._user_state.pop(user_id, None)
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": "❌ عملیات تغییر نام لغو شد."},
            )
            await self._send_file_list(chat_id, user_id, page=0)
        elif data.startswith("notes_"):
            page = int(data.split("_")[1])
            await self._send_notes_list(chat_id, user_id, page=page)
        elif data == "addnote_prompt":
            self._user_state[user_id] = {"action": "add_note"}
            keyboard = {"inline_keyboard": [[{"text": "❌ انصراف", "callback_data": "notes_0"}]]}
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "📝 *افزودن یادداشت جدید:*\n\nلطفاً متن یادداشت خود را در پیام بعدی تایپ و ارسال نمایید:",
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        elif data.startswith("viewnote_"):
            note_prefix = data.split("_")[1]
            await self._send_single_note(chat_id, user_id, note_prefix)
        elif data.startswith("delnote_"):
            note_prefix = data.split("_")[1]
            note = self._find_note_by_prefix(note_prefix)
            if note:
                await self.repo.delete_note(note["id"])
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": f"🗑️ یادداشت *«{note.get('title')}»* حذف گردید.", "parse_mode": "Markdown"},
                )
            await self._send_notes_list(chat_id, user_id, page=0)
        elif data == "cal_today":
            await self._send_calendar(chat_id, user_id)
        elif data == "daily_prompt":
            self._user_state[user_id] = {"action": "add_daily"}
            keyboard = {"inline_keyboard": [[{"text": "❌ انصراف", "callback_data": "cal_today"}]]}
            today_fa = format_full_jalali()
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"📅 *ثبت یادداشت روز ({today_fa}):*\n\nلطفاً متن یا برنامه امروز خود را بنویسید:",
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        elif data == "reminders_menu":
            await self._send_reminders_menu(chat_id, user_id)
        elif data == "addtask_prompt":
            self._user_state[user_id] = {"action": "add_reminder", "type": "task"}
            keyboard = {"inline_keyboard": [[{"text": "❌ انصراف", "callback_data": "reminders_menu"}]]}
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        "📌 *ثبت وظیفه / تسک جدید:*\n\n"
                        "لطفاً عنوان کار، و در صورت نیاز تاریخ شمسی و ساعت را بفرستید.\n\n"
                        "مثال‌ها:\n"
                        "• `جلسه هماهنگی پروژه`\n"
                        "• `1405/06/28 17:00 بررسی گزارش کار`"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        elif data == "addocc_prompt":
            self._user_state[user_id] = {"action": "add_reminder", "type": "occasion"}
            keyboard = {"inline_keyboard": [[{"text": "❌ انصراف", "callback_data": "reminders_menu"}]]}
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        "🎉 *ثبت مناسبت خاص روی تقویم شمسی:*\n\n"
                        "لطفاً عنوان مناسبت و تاریخ شمسی را ارسال کنید.\n\n"
                        "مثال:\n"
                        "`1405/07/15 سالگرد ازدواج`\n"
                        "`1405/08/10 جشن تولد آرمین`"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        elif data == "addtimer_prompt":
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "⏱️ ۵ دقیقه", "callback_data": "timer_5"},
                        {"text": "⏱️ ۱۰ دقیقه", "callback_data": "timer_10"},
                    ],
                    [
                        {"text": "⏱️ ۱۵ دقیقه", "callback_data": "timer_15"},
                        {"text": "⏱️ ۳۰ دقیقه", "callback_data": "timer_30"},
                    ],
                    [
                        {"text": "⏱️ ۱ ساعت", "callback_data": "timer_60"},
                        {"text": "⏱️ ۲ ساعت", "callback_data": "timer_120"},
                    ],
                    [{"text": "🔙 بازگشت به منو", "callback_data": "reminders_menu"}],
                ]
            }
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "⏳ *تنظیم سریع تایمر شمارش معکوس:*\n\nمدت زمان مورد نظر را انتخاب کنید:",
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
        elif data.startswith("timer_"):
            mins = int(data.split("_")[1])
            await self._create_quick_timer(chat_id, user_id, mins)
        elif data.startswith("donerem_"):
            rem_prefix = data.split("_")[1]
            rem = self._find_reminder_by_prefix(rem_prefix)
            if rem:
                await self.repo.toggle_reminder(rem["id"])
                new_state = not rem.get("is_completed", False)
                status_txt = "✅ انجام شد" if new_state else "🔄 به وضعیت فعال بازگشت"
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": f"{status_txt}: *«{rem.get('title')}»*", "parse_mode": "Markdown"},
                )
            await self._send_reminders_menu(chat_id, user_id)
        elif data.startswith("snooze_"):
            rem_prefix = data.split("_")[1]
            rem = self._find_reminder_by_prefix(rem_prefix)
            if rem:
                await self.repo.snooze_reminder(rem["id"], minutes=10)
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": f"⏳ یادآور *«{rem.get('title')}»* برای ۱۰ دقیقه به تعویق افتاد.", "parse_mode": "Markdown"},
                )
        elif data.startswith("delrem_"):
            rem_prefix = data.split("_")[1]
            rem = self._find_reminder_by_prefix(rem_prefix)
            if rem:
                await self.repo.delete_reminder(rem["id"])
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={"chat_id": chat_id, "text": f"🗑️ یادآور «{rem.get('title')}» حذف شد."},
                )
            await self._send_reminders_menu(chat_id, user_id)

    # --- Helpers --------------------------------------------------------------

    async def _send_file_to_chat(self, chat_id: int, user_id: int, node: dict[str, Any]) -> None:
        """Send a stored file directly to the Telegram chat as a document."""
        size = int(node.get("size_bytes", 0))
        name = node.get("name", "file")
        user = await self._get_or_create_user(user_id)
        token = self._mint_download_token(user)
        token_param = f"?token={token}" if token else ""
        download_url = f"{self.public_base_url}/api/v1/files/{node['id']}/content{token_param}"

        if size > 50 * 1024 * 1024:
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        f"⚠️ حجم فایل *«{name}»* ({format_bytes(size)}) بیشتر از سقف ۵۰ مگابایت تلگرام برای ربات‌ها است.\n\n"
                        "لطفاً جهت دانلود یا تماشای آنلاین، از لینک دانلود مستقیم استفاده فرمایید:"
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": "🌐 دانلود مستقیم در مرورگر", "url": download_url}]
                        ]
                    },
                },
            )
            return

        status_msg = await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"⏳ در حال آماده‌سازی و ارسال فایل *«{name}»* ({format_bytes(size)}) به تلگرام...",
                "parse_mode": "Markdown",
            },
        )
        status_id = None
        if status_msg.is_success:
            try:
                status_id = status_msg.json().get("result", {}).get("message_id")
            except Exception:
                pass

        try:
            file_bytes = b""
            if self.download_service:
                stream = self.download_service.stream(
                    node_id=node["id"],
                    owner_id=node["owner_id"],
                    byte_range=None,
                )
                chunks = []
                async for chunk in stream:
                    chunks.append(chunk)
                file_bytes = b"".join(chunks)

            if not file_bytes:
                async with httpx.AsyncClient(timeout=60.0) as cli:
                    resp = await cli.get(download_url)
                    if resp.is_success:
                        file_bytes = resp.content

            if not file_bytes:
                raise ValueError("محتوای فایل خالی است یا قابل واکشی نمی‌باشد.")

            mime = node.get("mime_type") or "application/octet-stream"
            files = {"document": (name, file_bytes, mime)}
            resp = await self._client.post(
                f"{self.api_base}/sendDocument",
                data={
                    "chat_id": chat_id,
                    "caption": f"✅ *فایل شما با موفقیت دریافت شد:*\n📄 `{name}`\n💾 حجم: `{format_bytes(len(file_bytes))}`",
                    "parse_mode": "Markdown",
                },
                files=files,
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
            if not resp.is_success:
                log.error("sendDocument error: %s", resp.text)
                await self._client.post(
                    f"{self.api_base}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"❌ ارسال مستقیم فایل به تلگرام با خطا مواجه شد. لطفاً از لینک دانلود مستقیم استفاده نمایید:\n{download_url}",
                    },
                )
            if status_id:
                try:
                    await self._client.post(
                        f"{self.api_base}/deleteMessage",
                        json={"chat_id": chat_id, "message_id": status_id},
                    )
                except Exception:
                    pass
        except Exception as e:
            log.exception("Error sending file to chat: %s", e)
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        f"❌ خطا در ارسال فایل: `{str(e)}`\n\n"
                        f"می‌توانید فایل را از طریق لینک مستقیم دانلود کنید:\n{download_url}"
                    ),
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": "🌐 دانلود مستقیم در مرورگر", "url": download_url}]
                        ]
                    },
                },
            )

    def _find_node_by_hex(self, hex_or_prefix: str) -> dict[str, Any] | None:
        nodes = getattr(self.repo, "nodes", {})
        try:
            import uuid
            direct_uuid = str(uuid.UUID(hex_or_prefix))
            if direct_uuid in nodes and nodes[direct_uuid].get("kind") == "file" and not nodes[direct_uuid].get("trashed_at"):
                return nodes[direct_uuid]
        except Exception:
            pass

        clean_target = hex_or_prefix.replace("-", "").lower()
        for node in nodes.values():
            node_id_str = str(node.get("id", ""))
            node_hex = node_id_str.replace("-", "").lower()
            if (
                node_hex == clean_target
                or node_hex.startswith(clean_target)
                or node_id_str.startswith(hex_or_prefix)
            ) and node.get("kind") == "file" and not node.get("trashed_at"):
                return node
        return None

    def _find_node_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        return self._find_node_by_hex(prefix)

    async def _get_or_create_user(self, telegram_user_id: int) -> dict[str, Any]:
        """Look up user by Telegram ID, auto-linking to the primary web account."""
        user = await self.repo.get_user_by_telegram_id(telegram_user_id)

        # Look for the primary web user in the system (e.g. user@teledrive.dev, user with files, or first non-tg user)
        primary_user = None
        users_list = list(getattr(self.repo, "users", {}).values())

        # 1. Look for user@teledrive.dev
        for u in users_list:
            if (u.get("email") or "").lower() == "user@teledrive.dev":
                primary_user = u
                break

        # 2. If not found, look for any user who owns active files
        if not primary_user and hasattr(self.repo, "nodes"):
            for u in users_list:
                if any(str(n.get("owner_id")) == str(u["id"]) for n in self.repo.nodes.values() if n.get("kind") == "file" and not n.get("trashed_at")):
                    primary_user = u
                    break

        # 3. If not found, look for any user whose email does NOT start with tg_
        if not primary_user:
            for u in users_list:
                if not (u.get("email") or "").startswith("tg_"):
                    primary_user = u
                    break

        # Check if user has active files
        user_has_files = False
        if user and hasattr(self.repo, "nodes"):
            user_has_files = any(
                str(n.get("owner_id")) == str(user["id"])
                for n in self.repo.nodes.values()
                if n.get("kind") == "file" and not n.get("trashed_at")
            )

        # Unify if user is an isolated dummy tg_ user OR user has no files while primary web user exists
        if user and primary_user and str(primary_user["id"]) != str(user["id"]) and (not user_has_files or (user.get("email") or "").startswith("tg_")):
            log.info("Unifying user %s into primary web user %s", user["id"], primary_user["id"])
            if hasattr(self.repo, "nodes"):
                for n in self.repo.nodes.values():
                    if str(n.get("owner_id")) == str(user["id"]):
                        n["owner_id"] = primary_user["id"]
            primary_user["telegram_user_id"] = telegram_user_id
            if hasattr(self.repo, "users"):
                if user["id"] in self.repo.users:
                    self.repo.users[user["id"]]["telegram_user_id"] = None
                if primary_user["id"] in self.repo.users:
                    self.repo.users[primary_user["id"]]["telegram_user_id"] = telegram_user_id
            user["telegram_user_id"] = None
            if hasattr(self.repo, "_save_state"):
                self.repo._save_state()
            return primary_user

        if user:
            return user

        # Auto-link primary web user if not linked yet
        if primary_user:
            log.info("Auto-linked primary user %s to telegram id %s", primary_user["id"], telegram_user_id)
            primary_user["telegram_user_id"] = telegram_user_id
            if hasattr(self.repo, "users") and primary_user["id"] in self.repo.users:
                self.repo.users[primary_user["id"]]["telegram_user_id"] = telegram_user_id
            if hasattr(self.repo, "_save_state"):
                self.repo._save_state()
            return primary_user

        # Single-tenant personal drive auto-link fallback
        if len(users_list) == 1:
            users_list[0]["telegram_user_id"] = telegram_user_id
            if hasattr(self.repo, "_save_state"):
                self.repo._save_state()
            return users_list[0]

        user = await self.repo.create_user(
            email=f"tg_{telegram_user_id}@teledrive.dev",
            password_hash=None,
            display_name=f"کاربر تلگرام {telegram_user_id}",
            telegram_user_id=telegram_user_id,
        )
        try:
            from app.core import crypto
            import uuid
            dek = crypto.generate_dek()
            wrapped = crypto.wrap_dek(
                dek, self.settings.master_kek, user_id_bytes=uuid.UUID(user["id"]).bytes
            )
            await self.repo.set_user_wrapped_dek(user["id"], wrapped, version=1)
        except Exception as e:
            log.warning("Could not wrap DEK for telegram user: %s", e)

        return user

    def _get_emoji(self, name: str) -> str:
        ext = name.split(".")[-1].lower() if "." in name else ""
        if ext in ("mp4", "mkv", "avi", "mov", "webm"):
            return "🎬"
        if ext in ("mp3", "wav", "flac", "ogg", "m4a"):
            return "🎵"
        if ext in ("jpg", "jpeg", "png", "webp", "gif"):
            return "🖼️"
        if ext in ("zip", "rar", "7z", "tar", "gz"):
            return "📦"
        if ext in ("pdf", "doc", "docx", "txt"):
            return "📄"
        if ext in ("apk", "exe", "msi"):
            return "🤖"
        return "📄"

    # --- Background Reminder Dispatcher ---------------------------------------

    async def _reminder_dispatcher_loop(self) -> None:
        """Periodic background loop checking due tasks/reminders and sending alerts."""
        log.info("TelegramBotService: Reminder dispatcher loop started.")
        while self._running:
            try:
                if self.bot_token and self._client and hasattr(self.repo, "list_due_reminders"):
                    now = datetime.now(timezone.utc)
                    due_items = await self.repo.list_due_reminders(now)
                    for item in due_items:
                        await self._dispatch_reminder_alert(item)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Reminder dispatcher check exception: %s", e)

            await asyncio.sleep(15)

    async def _dispatch_reminder_alert(self, item: dict[str, Any]) -> None:
        """Send a rich alarm notification to Telegram when a reminder/timer is due."""
        rem_id = item["id"]
        title = item.get("title", "وظیفه / رویداد")
        rem_type = item.get("type", "task")
        date_shamsi = item.get("date_shamsi") or ""
        time_str = item.get("time_str") or ""

        type_names = {
            "task": "📌 وظیفه / کار شخصی",
            "occasion": "🎉 مناسبت خاص / رویداد تقویم",
            "timer": "⏳ تایمر شمارش معکوس",
        }
        type_str = type_names.get(rem_type, "⏰ یادآور")

        chat_id = item.get("telegram_chat_id")
        if not chat_id and hasattr(self.repo, "users"):
            owner_id = str(item.get("owner_id"))
            for u in self.repo.users.values():
                if str(u.get("id")) == owner_id and u.get("telegram_user_id"):
                    chat_id = u["telegram_user_id"]
                    break
            if not chat_id:
                for u in self.repo.users.values():
                    if u.get("telegram_user_id"):
                        chat_id = u["telegram_user_id"]
                        break

        if not chat_id:
            return

        hex_id = rem_id.replace("-", "")[:12]
        text = (
            f"⏰ *هشدار و اعلان TeleDrive!*\n\n"
            f"📌 *عنوان:* `{title}`\n"
            f"🏷️ *نوع:* {type_str}\n"
        )
        if date_shamsi:
            text += f"📅 *تاریخ شمسی:* `{date_shamsi}`\n"
        if time_str:
            text += f"🕒 *زمان:* `{time_str}`\n"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ انجام شد", "callback_data": f"donerem_{hex_id}"},
                    {"text": "⏳ ۱۰ دقیقه بعد", "callback_data": f"snooze_{hex_id}"},
                ],
                [
                    {"text": "🗑️ حذف", "callback_data": f"delrem_{hex_id}"},
                    {"text": "📅 تقویم و رویدادها", "callback_data": "cal_today"},
                ],
            ]
        }

        try:
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
            repeat = item.get("repeat", "none")
            if repeat == "daily" and item.get("remind_at_utc"):
                item["remind_at_utc"] = item["remind_at_utc"] + timedelta(days=1)
                item["notified"] = False
                if hasattr(self.repo, "_save_state"):
                    self.repo._save_state()
            elif repeat == "weekly" and item.get("remind_at_utc"):
                item["remind_at_utc"] = item["remind_at_utc"] + timedelta(weeks=1)
                item["notified"] = False
                if hasattr(self.repo, "_save_state"):
                    self.repo._save_state()
            else:
                await self.repo.mark_reminder_notified(rem_id)
        except Exception as e:
            log.warning("Failed to dispatch reminder %s: %s", rem_id, e)

    # --- Notes & Daily Notes Helpers ------------------------------------------

    async def _send_notes_list(self, chat_id: int, user_id: int, page: int = 0) -> None:
        user = await self._get_or_create_user(user_id)
        owner_id = user["id"]
        notes = await self.repo.list_notes(owner_id=owner_id)

        if not notes:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "➕ ثبت یادداشت جدید", "callback_data": "addnote_prompt"}],
                    [{"text": "📅 یادداشت امروز", "callback_data": "daily_prompt"}],
                    [{"text": "🔙 بازگشت به منو", "callback_data": "start"}],
                ]
            }
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "📝 *هنوز یادداشتی ثبت نشده است.*\n\nمی‌توانید همین الان با دکمه زیر یادداشت یا برنامه روزانه خود را ثبت کنید:",
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
            )
            return

        PAGE_SIZE = 5
        total_pages = max(1, math.ceil(len(notes) / PAGE_SIZE))
        page = max(0, min(page, total_pages - 1))
        page_items = notes[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

        text = (
            f"📝 *یادداشت‌های من در TeleDrive*\n"
            f"📄 صفحه {page + 1} از {total_pages} (کل: {len(notes)} عدد)\n\n"
        )
        keyboard_rows = [
            [
                {"text": "➕ یادداشت جدید", "callback_data": "addnote_prompt"},
                {"text": "📅 یادداشت روزانه", "callback_data": "daily_prompt"},
            ]
        ]

        for idx, n in enumerate(page_items, start=page * PAGE_SIZE + 1):
            hex_id = n["id"].replace("-", "")[:12]
            title = n.get("title", "یادداشت")
            preview = (n.get("content", "").strip()[:40] or "...")
            date_info = f" ({n.get('date_shamsi')})" if n.get("date_shamsi") else ""
            badge = "📅 " if n.get("is_daily") else "📌 "

            text += f"{idx}. {badge}*{title}*{date_info}\n   _{preview}_\n\n"
            keyboard_rows.append([
                {"text": f"👁️ مشاهده: {title[:20]}", "callback_data": f"viewnote_{hex_id}"},
                {"text": "🗑️ حذف", "callback_data": f"delnote_{hex_id}"},
            ])

        nav_row = []
        if page > 0:
            nav_row.append({"text": "⬅️ قبلی", "callback_data": f"notes_{page - 1}"})
        if page < total_pages - 1:
            nav_row.append({"text": "بعدی ➡️", "callback_data": f"notes_{page + 1}"})
        if nav_row:
            keyboard_rows.append(nav_row)

        keyboard_rows.append([{"text": "🔙 بازگشت به منو", "callback_data": "start"}])

        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": keyboard_rows},
            },
        )

    async def _send_single_note(self, chat_id: int, user_id: int, note_prefix: str) -> None:
        note = self._find_note_by_prefix(note_prefix)
        if not note:
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": "❌ یادداشت مورد نظر یافت نشد."},
            )
            return

        title = note.get("title", "یادداشت")
        content = note.get("content", "")
        date_shamsi = note.get("date_shamsi", "")
        hex_id = note["id"].replace("-", "")[:12]

        date_line = f"📅 تاریخ: `{date_shamsi}`\n" if date_shamsi else ""
        text = (
            f"📝 *{title}*\n"
            f"{date_line}"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{content}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "🗑️ حذف یادداشت", "callback_data": f"delnote_{hex_id}"}],
                [{"text": "🔙 بازگشت به لیست", "callback_data": "notes_0"}],
            ]
        }
        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard},
        )

    async def _send_today_note(self, chat_id: int, user_id: int) -> None:
        user = await self._get_or_create_user(user_id)
        owner_id = user["id"]
        today_shamsi = format_jalali_date(*get_current_jalali())
        notes = await self.repo.list_notes(owner_id=owner_id, date_shamsi=today_shamsi)

        if notes:
            n = notes[0]
            hex_id = n["id"].replace("-", "")[:12]
            text = (
                f"📅 *یادداشت امروز ({format_full_jalali()}):*\n\n"
                f"*{n.get('title')}*\n\n"
                f"{n.get('content')}\n"
            )
            keyboard = {
                "inline_keyboard": [
                    [{"text": "➕ افزودن یادداشت دیگر", "callback_data": "daily_prompt"}],
                    [{"text": "🗑️ حذف این یادداشت", "callback_data": f"delnote_{hex_id}"}],
                    [{"text": "🔙 بازگشت", "callback_data": "cal_today"}],
                ]
            }
        else:
            text = (
                f"📅 *یادداشت امروز ({format_full_jalali()}):*\n\n"
                "برای امروز هنوز یادداشتی ثبت نکرده‌اید. با دکمه زیر می‌توانید متن یا برنامه روزانه خود را اضافه کنید:"
            )
            keyboard = {
                "inline_keyboard": [
                    [{"text": "➕ ثبت یادداشت امروز", "callback_data": "daily_prompt"}],
                    [{"text": "🔙 بازگشت", "callback_data": "cal_today"}],
                ]
            }

        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard},
        )

    # --- Calendar & Reminders Helpers -----------------------------------------

    async def _send_calendar(self, chat_id: int, user_id: int) -> None:
        user = await self._get_or_create_user(user_id)
        owner_id = user["id"]
        today_fa = format_full_jalali()
        today_shamsi = format_jalali_date(*get_current_jalali())

        today_notes = await self.repo.list_notes(owner_id=owner_id, date_shamsi=today_shamsi)
        today_reminders = await self.repo.list_reminders(owner_id=owner_id, date_shamsi=today_shamsi)

        text = (
            f"📅 *تقویم شمسی و رویدادهای روز*\n\n"
            f"✨ *امروز:* `{today_fa}`\n"
            f"📆 *تاریخ:* `{today_shamsi}`\n\n"
        )

        if today_notes:
            text += "📝 *یادداشت‌های امروز:*\n"
            for n in today_notes:
                text += f"• *{n.get('title')}*: _{n.get('content')[:40]}_\n"
            text += "\n"

        if today_reminders:
            text += "⏰ *وظایف و رویدادهای امروز:*\n"
            for r in today_reminders:
                status_icon = "✅" if r.get("is_completed") else "⏳"
                time_str = f" ({r.get('time_str')})" if r.get("time_str") else ""
                text += f"{status_icon} *{r.get('title')}*{time_str}\n"
            text += "\n"
        elif not today_notes:
            text += "📭 هیچ رویداد یا یادداشتی برای امروز ثبت نشده است.\n\n"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📝 یادداشت امروز", "callback_data": "daily_prompt"},
                    {"text": "📌 وظیفه جدید", "callback_data": "addtask_prompt"},
                ],
                [
                    {"text": "🎉 مناسبت جدید", "callback_data": "addocc_prompt"},
                    {"text": "⏳ تایمر معکوس", "callback_data": "addtimer_prompt"},
                ],
                [
                    {"text": "⏰ لیست همه یادآورها", "callback_data": "reminders_menu"},
                    {"text": "🔙 بازگشت به منو", "callback_data": "start"},
                ],
            ]
        }

        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "reply_markup": keyboard},
        )

    async def _send_reminders_menu(self, chat_id: int, user_id: int) -> None:
        user = await self._get_or_create_user(user_id)
        owner_id = user["id"]
        reminders = await self.repo.list_reminders(owner_id=owner_id, include_completed=True)

        text = "⏰ *مدیریت وظایف، مناسبت‌ها و یادآورها*\n\n"
        keyboard_rows = [
            [
                {"text": "📌 افزودن وظیفه", "callback_data": "addtask_prompt"},
                {"text": "🎉 ثبت مناسبت", "callback_data": "addocc_prompt"},
            ],
            [
                {"text": "⏳ تایمر سریع", "callback_data": "addtimer_prompt"},
                {"text": "📅 تقویم امروز", "callback_data": "cal_today"},
            ],
        ]

        if not reminders:
            text += "📭 در حال حاضر هیچ وظیفه یا یادآوری ثبت نشده است."
        else:
            text += "لیست کارهای فعال و مناسبت‌های پیش‌رو:\n\n"
            for r in reminders[:8]:
                hex_id = r["id"].replace("-", "")[:12]
                is_done = r.get("is_completed", False)
                icon = "✅" if is_done else ("🎉" if r.get("type") == "occasion" else "📌")
                date_str = f" [{r.get('date_shamsi')}]" if r.get("date_shamsi") else ""
                time_str = f" {r.get('time_str')}" if r.get("time_str") else ""
                strike = "~" if is_done else "*"

                text += f"{icon} {strike}{r.get('title')}{strike}{date_str}{time_str}\n"

                btn_toggle_text = "🔄 فعال‌سازی" if is_done else "✅ انجام شد"
                keyboard_rows.append([
                    {"text": btn_toggle_text, "callback_data": f"donerem_{hex_id}"},
                    {"text": "🗑️ حذف", "callback_data": f"delrem_{hex_id}"},
                ])

        keyboard_rows.append([{"text": "🔙 بازگشت به منو", "callback_data": "start"}])

        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": keyboard_rows},
            },
        )

    async def _handle_remind_command(self, chat_id: int, user_id: int, text: str) -> None:
        parts = text.replace("/remind", "").replace("/tasks", "").strip()
        if not parts:
            await self._send_reminders_menu(chat_id, user_id)
            return

        user = await self._get_or_create_user(user_id)
        owner_id = user["id"]

        # 1. Quick relative timer e.g. "10m تماس با شرکت" or "2h استراحت"
        words = parts.split(maxsplit=1)
        first_token = words[0].lower()
        timer_mins = None
        if first_token.endswith("m") and first_token[:-1].isdigit():
            timer_mins = int(first_token[:-1])
        elif first_token.endswith("h") and first_token[:-1].isdigit():
            timer_mins = int(first_token[:-1]) * 60

        if timer_mins and timer_mins > 0:
            title = words[1] if len(words) > 1 else "تایمر"
            remind_at_utc = datetime.now(timezone.utc) + timedelta(minutes=timer_mins)
            await self.repo.create_reminder(
                owner_id=owner_id,
                title=title,
                type="timer",
                remind_at_utc=remind_at_utc,
                telegram_chat_id=chat_id,
            )
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"⏳ تایمر برای *{timer_mins} دقیقه دیگر* با عنوان *«{title}»* تنظیم شد.\nدر سررسید زمان اعلان برای شما ارسال خواهد شد.",
                    "parse_mode": "Markdown",
                },
            )
            return

        # 2. Date + time + title e.g. "1405/06/27 15:30 جلسه کاری"
        parsed_date = parse_jalali_string(first_token)
        if parsed_date and len(words) > 1:
            rest = words[1].split(maxsplit=1)
            time_str = "09:00"
            title = words[1]
            if len(rest) == 2 and ":" in rest[0]:
                time_str = rest[0]
                title = rest[1]

            jy, jm, jd = parsed_date
            hour, minute = 9, 0
            if ":" in time_str:
                try:
                    p = time_str.split(":")
                    hour, minute = int(p[0]), int(p[1])
                except Exception:
                    pass
            remind_at_utc = jalali_to_datetime(jy, jm, jd, hour=hour, minute=minute)
            date_shamsi = format_jalali_date(jy, jm, jd)

            await self.repo.create_reminder(
                owner_id=owner_id,
                title=title,
                type="task",
                date_shamsi=date_shamsi,
                time_str=time_str,
                remind_at_utc=remind_at_utc,
                telegram_chat_id=chat_id,
            )
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"✅ وظیفه *«{title}»* برای تاریخ *{date_shamsi}* ساعت *{time_str}* ثبت شد.",
                    "parse_mode": "Markdown",
                },
            )
            return

        # 3. Simple text title -> add as today's task
        today_shamsi = format_jalali_date(*get_current_jalali())
        await self.repo.create_reminder(
            owner_id=owner_id,
            title=parts,
            type="task",
            date_shamsi=today_shamsi,
            telegram_chat_id=chat_id,
        )
        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"✅ وظیفه *«{parts}»* با موفقیت ثبت شد.",
                "parse_mode": "Markdown",
            },
        )

    async def _create_quick_timer(self, chat_id: int, user_id: int, minutes: int) -> None:
        user = await self._get_or_create_user(user_id)
        owner_id = user["id"]
        remind_at_utc = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        await self.repo.create_reminder(
            owner_id=owner_id,
            title=f"تایمر {minutes} دقیقه‌ای",
            type="timer",
            remind_at_utc=remind_at_utc,
            telegram_chat_id=chat_id,
        )
        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"⏱️ تایمر *{minutes} دقیقه‌ای* فعال شد. سر موعد به شما پیام داده خواهد شد.",
                "parse_mode": "Markdown",
            },
        )
        await self._send_reminders_menu(chat_id, user_id)

    async def _handle_interactive_text_input(
        self, chat_id: int, user_id: int, text: str, state: dict[str, Any]
    ) -> None:
        clean = text.strip()
        if clean in ("/cancel", "انصراف", "لغو"):
            self._user_state.pop(user_id, None)
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": "❌ عملیات لغو شد."},
            )
            return

        action = state.get("action")
        user = await self._get_or_create_user(user_id)
        owner_id = user["id"]

        if action == "add_note":
            self._user_state.pop(user_id, None)
            lines = clean.split("\n", 1)
            title = lines[0][:80]
            content = lines[1] if len(lines) > 1 else clean
            await self.repo.create_note(
                owner_id=owner_id,
                title=title,
                content=content,
                is_daily=False,
            )
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": f"✅ یادداشت *«{title}»* با موفقیت ذخیره شد.", "parse_mode": "Markdown"},
            )
            await self._send_notes_list(chat_id, user_id, page=0)

        elif action == "add_daily":
            self._user_state.pop(user_id, None)
            today_shamsi = format_jalali_date(*get_current_jalali())
            lines = clean.split("\n", 1)
            title = f"یادداشت روز {today_shamsi}"
            content = clean
            await self.repo.create_note(
                owner_id=owner_id,
                title=title,
                content=content,
                date_shamsi=today_shamsi,
                is_daily=True,
            )
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": f"✅ یادداشت روزانه برای *{today_shamsi}* با موفقیت ثبت شد.", "parse_mode": "Markdown"},
            )
            await self._send_calendar(chat_id, user_id)

        elif action == "add_reminder":
            self._user_state.pop(user_id, None)
            rem_type = state.get("type", "task")
            await self._handle_remind_command(chat_id, user_id, clean)

    def _find_note_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        clean = prefix.replace("-", "").lower()
        for n in getattr(self.repo, "notes", {}).values():
            n_id = str(n.get("id", "")).replace("-", "").lower()
            if n_id.startswith(clean):
                return n
        return None

    def _find_reminder_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        clean = prefix.replace("-", "").lower()
        for r in getattr(self.repo, "reminders", {}).values():
            r_id = str(r.get("id", "")).replace("-", "").lower()
            if r_id.startswith(clean):
                return r
        return None
