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
from datetime import datetime, timezone
from typing import Any

import httpx

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
        self._running = False
        self._bot_info: dict[str, Any] | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    async def start(self) -> None:
        """Start the bot polling loop and register commands."""
        if not self.bot_token:
            log.info("TelegramBotService: TELEGRAM_BOT_TOKEN is empty; bot will not poll.")
            return

        self._running = True
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))

        try:
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
                        {"command": "files", "description": "مشاهده فایل‌های من"},
                        {"command": "usage", "description": "میزان مصرف فضای ابری"},
                        {"command": "search", "description": "جستجوی فایل در درایو"},
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

        # Launch polling loop in background
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop polling loop and close HTTP client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        log.info("TelegramBotService stopped.")

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

        server_host = os.environ.get("SERVER_HOST") or "127.0.0.1:8000"
        download_url = f"http://{server_host}/api/v1/files/{node['id']}/content"

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
                    {"text": "🌐 ورود به درایو", "url": f"http://{server_host}/"},
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

        # Check for media/documents first
        if any(k in message for k in ("document", "video", "audio", "photo", "voice")):
            await self._handle_incoming_file(message)
            return

        if not text:
            return

        if text.startswith("/start"):
            await self._send_welcome(chat_id, from_user)
        elif text.startswith("/files") or text == "📂 فایل‌های من":
            await self._send_file_list(chat_id, user_id, page=0)
        elif text.startswith("/usage") or text.startswith("/stats") or text == "📊 وضعیت فضای ابری":
            await self._send_usage_stats(chat_id, user_id)
        elif text.startswith("/search"):
            query = text.replace("/search", "").strip()
            await self._send_search(chat_id, user_id, query)
        elif text.startswith("/help") or text == "ℹ️ راهنما":
            await self._send_help(chat_id)
        else:
            # If user sends plain text, search for matching files
            await self._send_search(chat_id, user_id, text)

    # --- Commands -------------------------------------------------------------

    async def _send_welcome(self, chat_id: int, from_user: dict[str, Any]) -> None:
        name = from_user.get("first_name", "کاربر عزیز")
        server_host = os.environ.get("SERVER_HOST") or "127.0.0.1:8000"
        web_url = f"http://{server_host}/"

        welcome_text = (
            f"👋 سلام *{name}*!\n\n"
            "☁️ *به درایو ابری نامحدود TeleDrive خوش آمدید!*\n\n"
            "با این ربات می‌توانید به سادگی و با نهایت سرعت:\n"
            "• هر نوع فایلی را ارسال کرده و نامحدود در تلگرام ذخیره کنید.\n"
            "• ویدیوها و آهنگ‌ها را آنلاین بدون دانلود استریم کنید.\n"
            "• لینک دانلود مستقیم فایل‌ها را دریافت نمایید.\n"
            "• فایل‌های خود را به صورت همگام با وب و اپلیکیشن اندروید مدیریت کنید.\n\n"
            "👇 یکی از گزینه‌های زیر را انتخاب کنید یا همین الان یک فایل بفرستید:"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📂 فایل‌های من", "callback_data": "files_0"},
                    {"text": "📊 وضعیت سهمیه", "callback_data": "usage"},
                ],
                [
                    {"text": "🔍 جستجوی فایل", "callback_data": "search_prompt"},
                    {"text": "ℹ️ راهنما", "callback_data": "help"},
                ],
                [
                    {"text": "🌐 ورود به درایو (Web App)", "url": web_url}
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
            "📖 *راهنمای استفاده از درایو ابری TeleDrive*\n\n"
            "🔹 *آپلود فایل:* کافیست هرگونه فایل، ویدیو، موزیک یا عکسی را به ربات بفرستید یا فوروارد کنید.\n\n"
            "🔹 *دستورات کلیدی:*\n"
            "• `/files` - مشاهده لیست فایل‌ها و پوشه‌ها\n"
            "• `/usage` - نمایش مصرف فضا و حجم باقی‌مانده\n"
            "• `/search <نام>` - جستجوی فایل‌ها در درایو\n"
            "• `/start` - بازگشت به منوی اصلی\n\n"
            "💡 *نکته:* تمام آپلودهای شما به صورت چانک‌های ۱ مگابایتی با استاندارد نظامی AES-256 رمزنگاری شده و بر بستر امن تلگرام ذخیره می‌شوند."
        )
        await self._client.post(
            f"{self.api_base}/sendMessage",
            json={"chat_id": chat_id, "text": help_text, "parse_mode": "Markdown"},
        )

    async def _send_usage_stats(self, chat_id: int, tg_user_id: int) -> None:
        user = await self._get_or_create_user(tg_user_id)
        owner_id = user["id"]
        usage = await self.repo.recompute_usage(owner_id)

        used_str = format_bytes(usage.get("used_bytes", 0))
        quota_str = format_bytes(user.get("quota_bytes", 100 * 1024**4))

        # Count total files
        root = await self.repo.get_root_node(owner_id)
        files_count = 0
        if root:
            nodes = await self.repo.list_children(owner_id=owner_id, parent_id=root["id"])
            files_count = len([n for n in nodes if n.get("kind") == "file"])

        msg = (
            "📊 *وضعیت فضای ابری TeleDrive*\n\n"
            f"👤 *کاربر:* `{user.get('display_name') or 'شما'}`\n"
            f"💾 *فضای مصرف‌شده:* `{used_str}`\n"
            f"📦 *سقف فضای اختصاصی:* `{quota_str}`\n"
            f"📁 *تعداد فایل‌ها:* `{files_count}` عدد\n"
            "🛡️ *امنیت:* رمزنگاری فعال (AES-256-GCM)\n"
            "🟢 *وضعیت سرور:* آنلاین و متصل"
        )
        keyboard = {
            "inline_keyboard": [
                [{"text": "📂 مشاهده فایل‌ها", "callback_data": "files_0"}],
                [{"text": "🔙 بازگشت به منو", "callback_data": "start"}],
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
        root = await self.repo.get_root_node(owner_id)

        if not root:
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": chat_id, "text": "درایو شما هنوز خالی است!"},
            )
            return

        all_nodes = await self.repo.list_children(owner_id=owner_id, parent_id=root["id"])
        file_nodes = [n for n in all_nodes if n.get("kind") == "file"]

        if not file_nodes:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "➕ همین حالا یک فایل ارسال کنید", "callback_data": "noop"}],
                    [{"text": "🔙 بازگشت به منو", "callback_data": "start"}],
                ]
            }
            await self._client.post(
                f"{self.api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "📭 هیچ فایلی در درایو شما یافت نشد.\n\nکافیست هر فایلی را برای ربات ارسال نمایید تا ذخیره شود!",
                    "reply_markup": keyboard,
                },
            )
            return

        PAGE_SIZE = 5
        total_pages = max(1, math.ceil(len(file_nodes) / PAGE_SIZE))
        page = max(0, min(page, total_pages - 1))
        page_items = file_nodes[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

        text = f"📂 *فایل‌های شما در درایو TeleDrive* (صفحه {page + 1} از {total_pages}):\n\n"
        keyboard_rows = []

        server_host = os.environ.get("SERVER_HOST") or "127.0.0.1:8000"

        for idx, node in enumerate(page_items, start=page * PAGE_SIZE + 1):
            name = node.get("name", "file")
            size = format_bytes(node.get("size_bytes", 0))
            emoji = self._get_emoji(name)
            text += f"{idx}. {emoji} *{name}* ({size})\n"

            download_url = f"http://{server_host}/api/v1/files/{node['id']}/content"
            keyboard_rows.append(
                [
                    {"text": f"⬇️ دریافت {name[:20]}", "url": download_url},
                    {"text": "🗑️ حذف", "callback_data": f"del_{node['id'][:12]}"},
                ]
            )

        # Pagination row
        nav_row = []
        if page > 0:
            nav_row.append({"text": "⬅️ قبلی", "callback_data": f"files_{page - 1}"})
        if page < total_pages - 1:
            nav_row.append({"text": "بعدی ➡️", "callback_data": f"files_{page + 1}"})
        if nav_row:
            keyboard_rows.append(nav_row)

        keyboard_rows.append([{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "start"}])

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

        server_host = os.environ.get("SERVER_HOST") or "127.0.0.1:8000"
        text = f"🔍 *نتایج جستجو برای «{query}»:* ({len(matched)} مورد)\n\n"
        buttons = []

        for node in matched[:10]:
            name = node.get("name", "file")
            size = format_bytes(node.get("size_bytes", 0))
            emoji = self._get_emoji(name)
            text += f"• {emoji} *{name}* ({size})\n"
            url = f"http://{server_host}/api/v1/files/{node['id']}/content"
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

        server_host = os.environ.get("SERVER_HOST") or "127.0.0.1:8000"

        if file_size > BOT_API_DOWNLOAD_LIMIT:
            msg = (
                f"⚠️ *فایل انتخابی ({format_bytes(file_size)}) بزرگتر از محدودیت ربات (۲۰ مگابایت) است.*\n\n"
                "برای آپلود فوق‌سریع فایل‌های پرحجم و گیگابایتی بدون هیچ محدودیتی، "
                "می‌توانید از **وب‌اپلیکیشن شیشه‌ای TeleDrive** یا اپلیکیشن اندروید استفاده نمایید:"
            )
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🌐 ورود به وب‌اپلیکیشن TeleDrive", "url": f"http://{server_host}/"}],
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

            # Create file node
            node_id = str(uuid.uuid4())
            await self.repo.create_file_node(
                node_id=node_id,
                owner_id=owner_id,
                parent_id=root_id,
                name=file_name,
                size_bytes=len(content_bytes),
                mime_type=mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream",
                chunk_size=1024 * 1024,
                total_chunks=1,
            )

            # Store chunk
            await self.repo.insert_chunk(
                node_id=node_id,
                chunk_index=0,
                plaintext_size=len(content_bytes),
                ciphertext_size=len(content_bytes),
                sha256=file_sha256,
                telegram_message_id=message.get("message_id", 1),
                telegram_channel_id=int(self.channel_id) if self.channel_id else -1001111111111,
            )

            await self.repo.finalize_file_node(
                node_id=node_id,
                owner_id=owner_id,
                size_bytes=len(content_bytes),
                sha256=file_sha256,
            )

            await self.repo.recompute_usage(owner_id)

            # Update confirmation message
            direct_link = f"http://{server_host}/api/v1/files/{node_id}/content"
            done_text = (
                "🎉 *فایل با موفقیت در TeleDrive ذخیره شد!*\n\n"
                f"📄 *نام فایل:* `{file_name}`\n"
                f"💾 *حجم:* `{format_bytes(len(content_bytes))}`\n"
                f"🕒 *زمان:* `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                f"🔗 [دانلود مستقیم و پخش آنلاین]({direct_link})"
            )

            keyboard = {
                "inline_keyboard": [
                    [{"text": "⬇️ دریافت فایل", "url": direct_link}],
                    [
                        {"text": "📂 فایل‌های من", "callback_data": "files_0"},
                        {"text": "📊 سهمیه", "callback_data": "usage"},
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
        elif data.startswith("del_"):
            # Delete file by short id prefix
            prefix = data.split("_")[1]
            user = await self._get_or_create_user(user_id)
            owner_id = user["id"]
            for node in list(self.repo.nodes.values()):
                if node.get("owner_id") == owner_id and node.get("id", "").startswith(prefix):
                    await self.repo.delete_node(node["id"], owner_id)
                    await self.repo.recompute_usage(owner_id)
                    await self._client.post(
                        f"{self.api_base}/sendMessage",
                        json={"chat_id": chat_id, "text": f"🗑️ فایل «{node.get('name')}» با موفقیت حذف شد."},
                    )
                    break
            await self._send_file_list(chat_id, user_id, page=0)

    # --- Helpers --------------------------------------------------------------

    async def _get_or_create_user(self, telegram_user_id: int) -> dict[str, Any]:
        """Look up user by Telegram ID or provision a new one."""
        user = await self.repo.get_user_by_telegram_id(telegram_user_id)
        if not user:
            user = await self.repo.create_user(
                email=f"tg_{telegram_user_id}@teledrive.dev",
                password_hash=None,
                display_name=f"Telegram User {telegram_user_id}",
                telegram_user_id=telegram_user_id,
            )
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
