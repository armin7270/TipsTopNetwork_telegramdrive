# راهنمای کامل دیپلوی TeleDrive روی Railway.com 🚂

این راهنما آموزش گام‌به‌گام و بسیار ساده راه‌اندازی پروژه **TeleDrive** روی پلتفرم ابری **Railway.com** است.

---

## ۱. پیش‌نیازها
1. حساب کاربری در [Railway.com](https://railway.com) (ورود رایگان با GitHub).
2. ریپازیتوری پروژه روی گیت‌هاب:
   `https://github.com/armin7270/TipsTopNetwork_telegramdrive`

---

## ۲. مراحل استقرار (Deploy) در Railway

### مرحله اول: ایجاد پروژه جدید
1. وارد پنل کاربری خود در **[railway.com/dashboard](https://railway.com/dashboard)** شوید.
2. دکمه **`+ New Project`** را بزنید.
3. گزینه **`Deploy from GitHub repo`** را انتخاب کنید.
4. ریپازیتوری `TipsTopNetwork_telegramdrive` را انتخاب نمایید.
5. دکمه **`Deploy Now`** را بزنید.

---

### مرحله دوم: تنظیم متغیرهای محیطی (Variables)
روی سرویس ایجادشده کلیک کرده و به تب **Variables** بروید. متغیرهای زیر را با کلیک روی **`+ New Variable`** وارد نمایید:

| نام متغیر (Key) | مقدار پیشنهادی (Value) | توضیحات |
| :--- | :--- | :--- |
| `TELEDRIVE_ENV` | `production` | محیط اجرایی پروداکشن |
| `TELEDRIVE_IN_MEMORY` | `1` | فعال‌سازی دیتابیس مستقل پایدار (JSON) |
| `TELEDRIVE_FAKE_TELEGRAM` | `0` | اتصال به تلگرام واقعی |
| `TELEGRAM_BOT_TOKEN` | `توکن ربات شما از BotFather` | اتصال به ربات تلگرام و نوتیفیکیشن‌ها |
| `STORAGE_POOL_CHANNEL_IDS` | `-100xxxxxxxxx` | شناسه عددی کانال خصوصی استوریج تلگرام |
| `STORAGE_CHUNK_SIZE_BYTES` | `1048576` | قطعات ۱ مگابایتی استاندارد |
| `CORS_ORIGINS` | `*` | اجازه دسترسی از تمام کلاینت‌ها و وب |
| `LOG_LEVEL` | `INFO` | سطح لاگ‌ها |

*(نکته: کلیدهای رمزنگاری `MASTER_KEK` و `JWT_SECRET` در صورت تعریف نشدن به صورت خودکار با مقادیر امن ساخته می‌شوند، اما می‌توانید در صورت تمایل مقادیر تصادفی خود را نیز وارد کنید).*

---

### مرحله سوم: ایجاد دیسک پایدار برای دیتابیس (Railway Volume)
برای اینکه دیتابیس کاربران و فایل‌ها (`data/teledrive_db.json`) با هر بار آپدیت پروژه در Railway حفظ شود:
1. در صفحه پروژه روی دکمه **`+ Create`** یا کلیک‌راست روی محیط پروژه بزنید.
2. گزینه **`Volume`** را انتخاب کنید.
3. نام ولوم را مشخص کنید و مسیر Mount Path آن را دقیقاً برابر با:
   ```text
   /app/data
   ```
   قرار دهید.
4. ولوم را به سرویس TeleDrive متصل (Attach) نمایید.

---

### مرحله چهارم: ساخت دامنه رایگان HTTPS (Domain Generation)
1. به تب **Settings** در سرویس TeleDrive بروید.
2. در بخش **Networking**، دکمه **`Generate Domain`** را بزنید.
3. یک دامنه با گواهینامه معتبر SSL خودکار (HTTPS) مانند زیر به شما اختصاص داده می‌شود:
   ```text
   https://teledrive-production.up.railway.app
   ```

---

## ۳. بررسی و تست پس از استقرار
- **صفحه اصلی وب‌اپلیکیشن:**
  `https://your-domain.up.railway.app/`
- **مستندات تعاملی Swagger API:**
  `https://your-domain.up.railway.app/docs`
- **بررسی سلامت سیستم (Health Check):**
  `https://your-domain.up.railway.app/readyz`
- **مشاهده لاگ‌های زنده:**
  به تب **Logs** در پنل Railway بروید و عملکرد زنده ربات و درخواست‌ها را مشاهده کنید.
