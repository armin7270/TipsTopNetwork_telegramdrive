#!/usr/bin/env bash
# ==============================================================================
# TeleDrive 1-Line Automated Installer for Ubuntu
# ==============================================================================

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

clear
echo -e "${CYAN}${BOLD}"
echo "  _____    _      ___       _           "
echo " |_   _|__| |___ |   \ _ _(_)_ _____    "
echo "   | |/ -_) / -_)| |) | '_| \ V / -_)   "
echo "   |_|\___|_\___||___/|_| |_|\_/\___|   "
echo "  Liquid Glass MTProto Cloud Storage    "
echo -e "${NC}"
echo -e "${YELLOW}>>> در حال شروع نصب و راه‌اندازی خودکار TeleDrive روی اوبونتو...${NC}\n"

# 1. Check Root Privileges
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[خطا] لطفاً این دستور را با دسترسی root یا sudo اجرا کنید:${NC}"
  echo -e "${YELLOW}sudo bash $0${NC}"
  exit 1
fi

# 2. Setup Target Directory
APP_DIR="/opt/telegram-drive"

# 3. Update System Packages & Install Core Tools
echo -e "${CYAN}[۱/۵] به‌روزرسانی پکیج‌های سیستم و نصب ابزارهای مورد نیاز...${NC}"
apt-get update -y
apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    gnupg \
    lsb-release \
    openssl \
    ufw

# Clone project if running standalone installer
if [ ! -f "Dockerfile" ]; then
    echo -e "${YELLOW}در حال دریافت سورس‌کد پروژه از گیت‌هاب...${NC}"
    if [ ! -d "$APP_DIR/.git" ]; then
        git clone https://github.com/armin7270/TipsTopNetwork_telegramdrive.git "$APP_DIR"
    else
        echo -e "${GREEN}✓ مخزن از قبل موجود است. در حال به‌روزرسانی...${NC}"
        git -C "$APP_DIR" pull origin main || true
    fi
    cd "$APP_DIR"
fi

# 4. Install Docker & Docker Compose if missing
echo -e "\n${CYAN}[۲/۵] بررسی و نصب موتور داکر (Docker Engine)...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}داکر یافت نشد. در حال نصب رسمی داکر...${NC}"
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo -e "${GREEN}✓ داکر با موفقیت نصب و فعال شد.${NC}"
else
    echo -e "${GREEN}✓ داکر از قبل نصب است.${NC}"
fi

# 5. Open Firewall Ports
echo -e "\n${CYAN}[۳/۵] تنظیم پورت‌های فایروال (UFW)...${NC}"
if ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp comment 'TeleDrive HTTP' || true
    ufw allow 443/tcp comment 'TeleDrive HTTPS' || true
    ufw allow 8000/tcp comment 'TeleDrive Direct' || true
    echo -e "${GREEN}✓ پورت‌های 80، 443 و 8000 باز شدند.${NC}"
else
    echo -e "${YELLOW}- فایروال غیرفعال است (رد شد).${NC}"
fi

# 6. Configure Environment (.env)
echo -e "\n${CYAN}[۴/۵] تنظیم اطلاعات ربات و کانال تلگرام...${NC}"

# Read inputs safely from TTY (works even with curl | bash)
BOT_TOKEN=""
CHANNEL_ID=""

if [ -t 0 ]; then
    read -r -p "لطفاً توکن ربات تلگرام خود را وارد کنید (اختیاری - اینتر برای بعد): " BOT_TOKEN
    read -r -p "لطفاً شناسه کانال ذخیره‌سازی تلگرام را وارد کنید (مثال: -1004351791022): " CHANNEL_ID
elif [ -e /dev/tty ]; then
    read -r -p "لطفاً توکن ربات تلگرام خود را وارد کنید (اختیاری - اینتر برای بعد): " BOT_TOKEN < /dev/tty || true
    read -r -p "لطفاً شناسه کانال ذخیره‌سازی تلگرام را وارد کنید (مثال: -1004351791022): " CHANNEL_ID < /dev/tty || true
fi

# Keep existing or fallback
if [ -f .env ]; then
    echo -e "${GREEN}✓ فایل .env از قبل موجود است و حفظ می‌شود.${NC}"
    if [ -n "$BOT_TOKEN" ]; then
        sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$BOT_TOKEN|" .env
    fi
    if [ -n "$CHANNEL_ID" ]; then
        sed -i "s|^STORAGE_POOL_CHANNEL_IDS=.*|STORAGE_POOL_CHANNEL_IDS=$CHANNEL_ID|" .env
    fi
else
    MASTER_KEY=$(openssl rand -base64 32)
    JWT_KEY=$(openssl rand -base64 32)

    cat <<EOF > .env
TELEDRIVE_ENV=production
TELEDRIVE_IN_MEMORY=1
TELEDRIVE_FAKE_TELEGRAM=0
MASTER_KEK=${MASTER_KEY}
JWT_SECRET=${JWT_KEY}
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
TELEGRAM_API_ID=6
TELEGRAM_API_HASH=eb06d4abfb49dc3eeb1aeb98ae0f581e
STORAGE_POOL_CHANNEL_IDS=${CHANNEL_ID}
STORAGE_CHUNK_SIZE_BYTES=1048576
MAX_CONCURRENT_UPLOADS=4
MAX_CONCURRENT_DOWNLOADS=8
LOG_LEVEL=INFO
CORS_ORIGINS=*
EOF
    echo -e "${GREEN}✓ فایل تنظیمات امنیتی .env تولید شد.${NC}"
fi

# 7. Start Containers
echo -e "\n${CYAN}[۵/۵] در حال بیلد و اجرای کانتینر درایو...${NC}"
mkdir -p data
docker compose down 2>/dev/null || true
docker compose up -d --build

SERVER_IP=$(curl -s -4 ifconfig.me || hostname -I | awk '{print $1}')

echo -e "\n${GREEN}${BOLD}====================================================${NC}"
echo -e "${GREEN}${BOLD}       🎉 درایو ابری TeleDrive با موفقیت راه‌اندازی شد!       ${NC}"
echo -e "${GREEN}${BOLD}====================================================${NC}"
echo -e "آدرس وب‌اپلیکیشن شیشه‌ای: ${CYAN}${BOLD}http://${SERVER_IP}:8000/${NC}"
echo -e "مستندات تعاملی API:       ${CYAN}http://${SERVER_IP}:8000/docs${NC}"
echo -e "بررسی وضعیت سرور:         ${CYAN}http://${SERVER_IP}:8000/readyz${NC}"
echo -e "\nدستور مشاهده زنده لاگ‌ها:  ${YELLOW}docker compose logs -f${NC}"
echo -e "دستور ریستارت سرویس:       ${YELLOW}docker compose restart${NC}"
echo -e "${GREEN}====================================================${NC}\n"
