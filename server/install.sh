#!/usr/bin/env bash
# Dropbear Slurry — server install script
# Tested on Ubuntu 24.04 LTS (x86_64 + ARM64)
# Run as root: sudo bash install.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/dropbearslurry/DropbearGrow.git"
INSTALL_DIR="/opt/dropbear"
SERVICE_USER="dropbear"
SERVICE_NAME="dropbear"
PORT=7331

# ── Colours ───────────────────────────────────────────────────────────────────
GRN='\033[0;32m'; CYN='\033[0;36m'; YLW='\033[1;33m'; RED='\033[0;31m'; RST='\033[0m'
step()  { echo -e "\n${CYN}▸ $*${RST}"; }
ok()    { echo -e "${GRN}  ✓ $*${RST}"; }
warn()  { echo -e "${YLW}  ⚠ $*${RST}"; }
fatal() { echo -e "${RED}  ✗ $*${RST}"; exit 1; }

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || fatal "Run as root: sudo bash install.sh"

echo -e "${CYN}"
echo "                                    /\\   /\\"
echo "                                   (  ) (  )"
echo "                                  ( O . O )"
echo "                                  (   w   )"
echo "                                   '-._.-'"
echo "                                     /|"
echo "                                    / |"
echo "                                   /  [~]"
echo ""
echo "  ██████╗ ██████╗  ██████╗ ██████╗ ██████╗ ███████╗ █████╗ ██████╗ "
echo "  ██╔══██╗██╔══██╗██╔═══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗██╔══██╗"
echo "  ██║  ██║██████╔╝██║   ██║██████╔╝██████╔╝█████╗  ███████║██████╔╝"
echo "  ██║  ██║██╔══██╗██║   ██║██╔═══╝ ██╔══██╗██╔══╝  ██╔══██║██╔══██╗"
echo "  ██████╔╝██║  ██║╚██████╔╝██║     ██████╔╝███████╗██║  ██║██║  ██║"
echo "  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝"
echo -e "${RST}"
echo "  SLURRY  ·  Server install  ·  Ubuntu 24.04 LTS"
echo ""

# ── System update ─────────────────────────────────────────────────────────────
step "Updating system packages"
apt-get update -qq
apt-get upgrade -y -qq
ok "System up to date"

# ── Dependencies ──────────────────────────────────────────────────────────────
step "Installing dependencies"
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    git \
    nginx \
    certbot \
    python3-certbot-nginx \
    curl \
    ufw
ok "Dependencies installed"

# ── Cloudflared ───────────────────────────────────────────────────────────────
step "Installing cloudflared"
if ! command -v cloudflared &>/dev/null; then
    ARCH=$(dpkg --print-architecture)
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb" \
        -o /tmp/cloudflared.deb
    dpkg -i /tmp/cloudflared.deb
    rm /tmp/cloudflared.deb
    ok "cloudflared installed"
else
    ok "cloudflared already installed ($(cloudflared --version))"
fi

# ── Service user ──────────────────────────────────────────────────────────────
step "Creating service user: ${SERVICE_USER}"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "User created"
else
    ok "User already exists"
fi

# ── Clone / update repo ───────────────────────────────────────────────────────
step "Cloning repository to ${INSTALL_DIR}"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    git -C "$INSTALL_DIR" pull --ff-only
    ok "Repository updated"
else
    git clone "$REPO_URL" "$INSTALL_DIR"
    ok "Repository cloned"
fi

# ── Python venv ───────────────────────────────────────────────────────────────
step "Setting up Python environment"
VENV="${INSTALL_DIR}/server/.venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "${INSTALL_DIR}/server/requirements.txt"
ok "Python environment ready"

# ── Directory structure ───────────────────────────────────────────────────────
step "Creating upload directories"
for cat in general receipts marketing production assets; do
    mkdir -p "${INSTALL_DIR}/server/uploads/${cat}"
done
ok "Upload directories ready"

# ── Internal web interface ────────────────────────────────────────────────────
step "Installing internal web interface"
mkdir -p "${INSTALL_DIR}/server/static"
cp "${INSTALL_DIR}/internal.html" "${INSTALL_DIR}/server/static/index.html"
ok "Internal page ready (served at /)"

# ── .env file ─────────────────────────────────────────────────────────────────
step "Checking .env configuration"
ENV_FILE="${INSTALL_DIR}/server/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "${INSTALL_DIR}/server/.env.example" "$ENV_FILE"
    warn ".env created from example — edit ${ENV_FILE} before starting the service"
else
    ok ".env already exists"
fi

# ── Permissions ───────────────────────────────────────────────────────────────
step "Setting permissions"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"
chmod 750 "${INSTALL_DIR}/server"
chmod 640 "$ENV_FILE"
ok "Permissions set"

# ── Systemd service ───────────────────────────────────────────────────────────
step "Installing systemd service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Dropbear Slurry — team server
After=network.target
Wants=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}/server
ExecStart=${VENV}/bin/python server.py
Restart=always
RestartSec=5
EnvironmentFile=${ENV_FILE}

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}/server

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
ok "Service installed and enabled"

# ── Nginx ─────────────────────────────────────────────────────────────────────
step "Configuring nginx"
cat > "/etc/nginx/sites-available/${SERVICE_NAME}" <<EOF
server {
    listen 80;
    server_name _;

    # WebSocket + HTTP proxy to uvicorn
    location / {
        proxy_pass         http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       \$host;
        proxy_set_header   X-Real-IP  \$remote_addr;
        proxy_read_timeout 86400;
    }
}
EOF

ln -sf "/etc/nginx/sites-available/${SERVICE_NAME}" "/etc/nginx/sites-enabled/${SERVICE_NAME}"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
ok "nginx configured"

# ── Firewall ──────────────────────────────────────────────────────────────────
step "Configuring firewall"
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
ok "Firewall enabled (SSH + HTTP/HTTPS open)"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GRN}  ╔══════════════════════════════════════════╗"
echo -e "  ║        Install complete                  ║"
echo -e "  ╚══════════════════════════════════════════╝${RST}"
echo ""
echo "  Next steps:"
echo ""
echo -e "  1. Edit your config:  ${YLW}nano ${ENV_FILE}${RST}"
echo "     (set SMTP_PASS and ADMIN_KEY)"
echo ""
echo -e "  2. Start the server:  ${YLW}systemctl start ${SERVICE_NAME}${RST}"
echo -e "     Check status:      ${YLW}systemctl status ${SERVICE_NAME}${RST}"
echo -e "     Live logs:         ${YLW}journalctl -fu ${SERVICE_NAME}${RST}"
echo ""
echo "  3. For TLS — choose one:"
echo ""
echo -e "     A) Own domain:     ${YLW}certbot --nginx -d yourdomain.com.au${RST}"
echo ""
echo -e "     B) Cloudflare Tunnel (no domain needed):"
echo -e "        ${YLW}cloudflared tunnel login${RST}"
echo -e "        ${YLW}cloudflared tunnel create dropbear${RST}"
echo -e "        ${YLW}cloudflared tunnel route dns dropbear yourdomain.com.au${RST}"
echo -e "        ${YLW}cloudflared tunnel run --url http://localhost:${PORT} dropbear${RST}"
echo ""
echo "  Once TLS is running, update internal.html default to wss://yourdomain"
echo ""
