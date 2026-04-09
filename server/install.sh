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
step "Configuring .env"
ENV_FILE="${INSTALL_DIR}/server/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    # Prompt for SMTP password (input hidden)
    echo ""
    echo -e "  Enter the SMTP password for ${CYN}admin@dropbearslurry.com.au${RST}:"
    read -r -s -p "  SMTP_PASS: " _smtp_pass
    echo ""

    # Generate a random ADMIN_KEY
    _admin_key=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

    cat > "$ENV_FILE" <<ENVEOF
SMTP_HOST=mail.dropbearslurry.com.au
SMTP_PORT=587
SMTP_USER=admin@dropbearslurry.com.au
SMTP_PASS=${_smtp_pass}
SMTP_FROM=internal@dropbearslurry.com.au
ADMIN_KEY=${_admin_key}
ENVEOF

    ok ".env written (SMTP_PASS set, ADMIN_KEY auto-generated)"
    echo -e "  ${YLW}ADMIN_KEY:${RST} ${_admin_key}"
    echo -e "  ${YLW}Save the ADMIN_KEY above — you'll need it to create accounts.${RST}"
else
    ok ".env already exists — skipping credential prompt"
fi

# ── Permissions ───────────────────────────────────────────────────────────────
step "Setting permissions"
# Code and config stay root-owned; the service only gets write access to what it actually writes
chown -R root:root "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"

# .env readable by service user, not world
chown root:"${SERVICE_USER}" "$ENV_FILE"
chmod 640 "$ENV_FILE"

# Service user owns only the runtime-writable directories/files
UPLOAD_PATH="${INSTALL_DIR}/server/uploads"
STATE_FILE="${INSTALL_DIR}/server/accounts.json"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "$UPLOAD_PATH"
chmod 750 "$UPLOAD_PATH"

# Pre-create accounts.json so it is owned correctly from the start
[[ -f "$STATE_FILE" ]] || echo '{}' > "$STATE_FILE"
chown "${SERVICE_USER}:${SERVICE_USER}" "$STATE_FILE"
chmod 640 "$STATE_FILE"

ok "Permissions set (code root-owned, uploads + accounts.json service-writable)"

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
ExecStart=${VENV}/bin/uvicorn server:app --host 127.0.0.1 --port ${PORT} --workers 1
Restart=always
RestartSec=5
EnvironmentFile=${ENV_FILE}

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}/server/uploads ${INSTALL_DIR}/server/accounts.json

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
if systemctl is-active --quiet nginx; then
    systemctl reload nginx
    ok "nginx configured and reloaded"
else
    systemctl start nginx
    ok "nginx configured and started"
fi

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
echo -e "  1. Start the server:  ${YLW}systemctl start ${SERVICE_NAME}${RST}"
echo -e "     Check status:      ${YLW}systemctl status ${SERVICE_NAME}${RST}"
echo -e "     Live logs:         ${YLW}journalctl -fu ${SERVICE_NAME}${RST}"
echo ""
echo "  2. For TLS — choose one:"
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
