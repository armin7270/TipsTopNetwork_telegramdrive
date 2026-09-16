#!/usr/bin/env bash
# ==============================================================================
# TeleDrive 1-Line Automated Installer for Ubuntu (Fingilish / English for SSH)
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
echo -e "${YELLOW}>>> Dar hale shorooe nasb va rah-andazi khodkare TeleDrive rooye Ubuntu...${NC}\n"

# 1. Check Root Privileges
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[ERROR] Lotfan in dastoor ra ba dastresi root ya sudo ejra konid:${NC}"
  echo -e "${YELLOW}sudo bash $0${NC}"
  exit 1
fi

# 2. Setup Target Directory
APP_DIR="/opt/telegram-drive"

# 3. Update System Packages & Install Core Tools
echo -e "${CYAN}[1/5] Be-rooz-resani package-ha va nasbe abzare morede niaz...${NC}"
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
    echo -e "${YELLOW}Dar hale daryafte source-code project az GitHub...${NC}"
    mkdir -p "$APP_DIR"
    if [ ! -d "$APP_DIR/.git" ]; then
        git clone https://github.com/armin7270/TipsTopNetwork_telegramdrive.git "$APP_DIR"
    else
        echo -e "${GREEN}✓ Makhzan az ghabl mojood ast. Dar hale update kardan...${NC}"
        git -C "$APP_DIR" pull origin main || true
    fi
    cd "$APP_DIR"
fi

# 4. Install Docker & Docker Compose if missing
echo -e "\n${CYAN}[2/5] Barresi va nasbe Docker Engine...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}Docker yaft nashod. Dar hale nasbe rasmi Docker...${NC}"
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo -e "${GREEN}✓ Docker ba movafaghiat nasb va faal shod.${NC}"
else
    echo -e "${GREEN}✓ Docker az ghabl nasb ast.${NC}"
fi

# 5. Open Firewall Ports
echo -e "\n${CYAN}[3/5] Tanzim port-haye firewall (UFW)...${NC}"
if ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp comment 'TeleDrive HTTP' || true
    ufw allow 443/tcp comment 'TeleDrive HTTPS' || true
    ufw allow 8000/tcp comment 'TeleDrive Direct' || true
    echo -e "${GREEN}✓ Port-haye 80, 443 va 8000 baz shodand.${NC}"
else
    echo -e "${YELLOW}- Firewall gheyr-faal ast (rad shod).${NC}"
fi

# 6. Configure Environment (.env)
echo -e "\n${CYAN}[4/5] Tanzime ettelaate Bot va Channel Telegram...${NC}"

# Read inputs safely from TTY (works even with curl | bash)
BOT_TOKEN=""
CHANNEL_ID=""

if [ -t 0 ]; then
    read -r -p "[?] Lotfan Token Bot Telegram ra vared konid (Ekhtiari - Enter baraye badeh): " BOT_TOKEN
    read -r -p "[?] Lotfan Shenase Channel Storage ra vared konid (Mesal: -1004351791022 - ya Enter baraye badeh): " CHANNEL_ID
elif [ -e /dev/tty ]; then
    read -r -p "[?] Lotfan Token Bot Telegram ra vared konid (Ekhtiari - Enter baraye badeh): " BOT_TOKEN < /dev/tty || true
    read -r -p "[?] Lotfan Shenase Channel Storage ra vared konid (Mesal: -1004351791022 - ya Enter baraye badeh): " CHANNEL_ID < /dev/tty || true
fi

# Keep existing or fallback
if [ -f .env ]; then
    echo -e "${GREEN}✓ File .env az ghabl mojood ast va hefz mishavad.${NC}"
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
    echo -e "${GREEN}✓ File tanzimate amniati .env sakhte shod.${NC}"
fi

# 7. Start Containers
echo -e "\n${CYAN}[5/5] Dar hale build va ejraye container TeleDrive...${NC}"
mkdir -p data
docker compose down 2>/dev/null || true
docker compose up -d --build

SERVER_IP=$(curl -s -4 ifconfig.me || hostname -I | awk '{print $1}')

echo -e "\n${GREEN}${BOLD}====================================================${NC}"
echo -e "${GREEN}${BOLD}   🎉 TeleDrive Cloud Storage Ba Movafaghiat Nasb Shod!   ${NC}"
echo -e "${GREEN}${BOLD}====================================================${NC}"
echo -e "Address Web App (Liquid Glass): ${CYAN}${BOLD}http://${SERVER_IP}:8000/${NC}"
echo -e "Mostanadate API (Swagger):       ${CYAN}http://${SERVER_IP}:8000/docs${NC}"
echo -e "Barresi Vaziate Server (Health): ${CYAN}http://${SERVER_IP}:8000/readyz${NC}"
echo -e "\nDastoore moshahedeye live log-ha:  ${YELLOW}docker compose logs -f${NC}"
echo -e "Dastoore restart kardane service:  ${YELLOW}docker compose restart${NC}"
echo -e "${GREEN}====================================================${NC}\n"
