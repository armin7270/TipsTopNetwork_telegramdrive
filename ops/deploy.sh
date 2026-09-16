#!/usr/bin/env bash
# ==============================================================================
# TeleDrive 1-Click Production Deployment Script for Ubuntu
# Tested on: Ubuntu 20.04, 22.04, 24.04 LTS
# ==============================================================================

set -e

# ANSI Color Codes
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}====================================================${NC}"
echo -e "${CYAN}     TeleDrive MTProto Cloud Storage Deployer       ${NC}"
echo -e "${CYAN}====================================================${NC}"

# Check Root Privileges
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[ERROR] Please run this script with root or sudo privileges: sudo bash $0${NC}"
  exit 1
fi

echo -e "\n${YELLOW}[1/5] Updating Ubuntu packages and installing dependencies...${NC}"
apt-get update -y
apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    gnupg \
    lsb-release \
    ufw

# Install Docker & Docker Compose if not present
echo -e "\n${YELLOW}[2/5] Checking Docker installation...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${CYAN}Installing official Docker Engine...${NC}"
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo -e "${GREEN}Docker installed successfully!${NC}"
else
    echo -e "${GREEN}Docker is already installed.${NC}"
fi

# Configure Firewall (UFW)
echo -e "\n${YELLOW}[3/5] Configuring firewall rules...${NC}"
if ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp comment 'TeleDrive HTTP' || true
    ufw allow 443/tcp comment 'TeleDrive HTTPS' || true
    ufw allow 8000/tcp comment 'TeleDrive Port' || true
    echo -e "${GREEN}Firewall rules applied.${NC}"
else
    echo -e "${CYAN}UFW is inactive; skipping firewall modification.${NC}"
fi

# Environment Configuration
echo -e "\n${YELLOW}[4/5] Checking environment settings (.env)...${NC}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo -e "${YELLOW}Created .env from .env.example.${NC}"
    else
        echo -e "${YELLOW}Creating default .env...${NC}"
        cat <<EOF > .env
TELEDRIVE_ENV=production
TELEDRIVE_IN_MEMORY=1
TELEDRIVE_FAKE_TELEGRAM=0
MASTER_KEK=$(openssl rand -base64 32)
JWT_SECRET=$(openssl rand -base64 32)
TELEGRAM_BOT_TOKEN=
TELEGRAM_API_ID=6
TELEGRAM_API_HASH=eb06d4abfb49dc3eeb1aeb98ae0f581e
STORAGE_POOL_CHANNEL_IDS=
STORAGE_CHUNK_SIZE_BYTES=1048576
MAX_CONCURRENT_UPLOADS=4
MAX_CONCURRENT_DOWNLOADS=8
LOG_LEVEL=INFO
CORS_ORIGINS=*
EOF
    fi
    echo -e "${YELLOW}NOTE: Edit .env to set your TELEGRAM_BOT_TOKEN and STORAGE_POOL_CHANNEL_IDS.${NC}"
fi

# Launch Containers via Docker Compose
echo -e "\n${YELLOW}[5/5] Building and launching TeleDrive container...${NC}"
mkdir -p data
docker compose down || true
docker compose up -d --build

# Retrieve IP Address
SERVER_IP=$(curl -s -4 ifconfig.me || hostname -I | awk '{print $1}')

echo -e "\n${GREEN}====================================================${NC}"
echo -e "${GREEN}       🎉 TeleDrive Successfully Deployed!         ${NC}"
echo -e "${GREEN}====================================================${NC}"
echo -e "Web Interface & PWA:  ${CYAN}http://${SERVER_IP}:8000/${NC}"
echo -e "Interactive Docs:     ${CYAN}http://${SERVER_IP}:8000/docs${NC}"
echo -e "Health Check:         ${CYAN}http://${SERVER_IP}:8000/readyz${NC}"
echo -e "Logs:                 ${CYAN}docker logs -f teledrive_server${NC}"
echo -e "Restart:              ${CYAN}docker compose restart${NC}"
echo -e "${GREEN}====================================================${NC}\n"
