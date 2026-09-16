# راهنمای استقرار TeleDrive روی سرور مجازی اوبونتو (Ubuntu VPS)

این راهنما مراحل کامل راه‌اندازی و اجرای درایو ابری **TeleDrive** را بر روی توزیع‌های اوبونتو (Ubuntu 20.04, 22.04, 24.04) توضیح می‌دهد.

---

## 📋 حداقل مشخصات سرور مورد نیاز (System Requirements)
- **پردازنده (CPU):** حداقل ۱ هسته (1 vCPU)
- **رم (RAM):** حداقل ۱ گیگابایت (1 GB RAM)
- **دیسک (Disk):** ۱۰ گیگابایت فضای خالی
- **سیستم‌عامل:** Ubuntu 22.04 LTS یا 24.04 LTS (پیشنهادی)
- **لوکیشن سرور:** توصیه می‌شود از سرورهای خارج از ایران (آلمان، هلند، فنلاند و...) استفاده کنید تا اتصال MTProto به سرورهای تلگرام بدون محدودیت فیلترینگ برقرار باشد.

---

## 🚀 روش اول: نصب سریع و خودکار با داکر (پیشنهادی و ۱-کلیک)

### مرحله ۱: اتصال به سرور از طریق SSH
ترمینال یا نرم‌افزار Putty را باز کرده و با کاربر `root` به سرور متصل شوید:
```bash
ssh root@YOUR_SERVER_IP
```

### مرحله ۲: دریافت فایل‌های پروژه
اگر پروژه را در گیت‌هاب دارید:
```bash
cd /opt
git clone <آدرس_مخزن_شما> telegram-drive
cd telegram-drive
```
یا در صورتی که فایل‌ها را مستقیماً منتقل می‌کنید، پوشه پروژه را در مسیر `/opt/telegram-drive` قرار دهید.

### مرحله ۳: اجرای اسکریپت نصب خودکار
اسکریپت آماده تمام پکیج‌های لازم، داکر و فایروال را به صورت خودکار نصب و تنظیم می‌کند:
```bash
chmod +x ops/deploy.sh
bash ops/deploy.sh
```

### مرحله ۴: تنظیم توکن بات و کانال تلگرام
فایل `.env` را باز کنید:
```bash
nano .env
```
مقادیر زیر را با اطلاعات بات و کانال خود پر کنید:
```env
TELEGRAM_BOT_TOKEN=8917143727:AAFnW9lFjC1lnaPJNomNlgwwz7WWennR_PU
STORAGE_POOL_CHANNEL_IDS=-1004351791022
```
سپس با زدن کلیدهای `Ctrl + O` و اینتر ذخیره کرده و با `Ctrl + X` خارج شوید.

برای اعمال تغییرات:
```bash
docker compose restart
```

اکنون با مرورگر به آدرس `http://YOUR_SERVER_IP:8000` بروید تا پنل درایو شیشه‌ای لود شود!

---

## 🛠️ روش دوم: نصب مستقیم بدون داکر (با Systemd و Python venv)

اگر تمایل به استفاده از داکر ندارید و می‌خواهید سرویس به صورت مستقیم در لینوکس اجرا شود:

### ۱. نصب پکیج‌های سیستمی
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git curl
```

### ۲. ایجاد محیط مجازی پایتون
```bash
cd /opt/telegram-drive
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### ۳. آماده‌سازی فایل تنظیمات `.env`
```bash
cp .env.example .env
nano .env
```
(اطلاعات `TELEGRAM_BOT_TOKEN` و `STORAGE_POOL_CHANNEL_IDS` را وارد نمایید).

### ۴. راه‌اندازی سرویس Systemd (اجرای دائم در پس‌زمینه)
```bash
sudo cp ops/teledrive.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable teledrive
sudo systemctl start teledrive
```

برای مشاهده وضعیت و لاگ‌ها:
```bash
sudo systemctl status teledrive
journalctl -u teledrive -f
```

---

## 🌐 روش اتصال دامنه و فعال‌سازی SSL رایگان (HTTPS با Nginx)

برای استفاده از پروتکل امن HTTPS و نصب PWA بر روی گوشی و لپ‌تاپ:

### ۱. نصب وب‌سرور Nginx و Certbot
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

### ۲. کپی تنظیمات Nginx
```bash
sudo cp ops/nginx.conf /etc/nginx/sites-available/teledrive
sudo nano /etc/nginx/sites-available/teledrive
```
مقدار `your_domain_or_ip` را به نام دامنه خود (مثلاً `drive.yourdomain.com`) تغییر دهید.

سپس سایت را فعال کرده و Nginx را ریستارت کنید:
```bash
sudo ln -s /etc/nginx/sites-available/teledrive /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

### ۳. دریافت گواهینامه رایگان SSL با Let's Encrypt
مطمئن شوید رکورد A دامنه شما به IP سرور اشاره می‌کند، سپس دستور زیر را بزنید:
```bash
sudo certbot --nginx -d drive.yourdomain.com
```
پاسخ به سوالات را تایید کنید. اکنون سایت شما با آدرس `https://drive.yourdomain.com` به صورت کاملاً امن با نماد قفل سبز در دسترس است!

---

## 📌 دستورات کاربردی برای مدیریت سرور (Cheatsheet)

| دستور | کارکرد |
|---|---|
| `docker compose logs -f` | مشاهده زنده لاگ‌های سرور |
| `docker compose restart` | ریستارت کانتینر درایو |
| `docker compose ps` | مشاهده وضعیت سلامتی کانتینر |
| `sudo systemctl restart nginx` | ریستارت وب‌سرور Nginx |
| `curl http://localhost:8000/readyz` | تست سلامت سرویس از داخل سرور |
