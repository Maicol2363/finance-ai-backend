#!/usr/bin/env bash
# ──────────────────────────────────────────────
# Finance AI Backend — VM Bootstrap Script
# ──────────────────────────────────────────────
# Run ONCE on a fresh Ubuntu 24.04 Azure VM:
#
#   ssh finance-admin@<VM-IP>
#   sudo bash /opt/finance-ai/deploy/setup-vm.sh
#
# ──────────────────────────────────────────────
set -euo pipefail

# ── Config ──
APP_USER="finance-ai"
APP_DIR="/opt/finance-ai"
REPO_URL="https://github.com/Maicol2363/finance-ai-backend.git"

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; }

if [[ $EUID -ne 0 ]]; then
    err "Run as root: sudo bash setup-vm.sh"
    exit 1
fi

echo "========================================"
echo " Finance AI Backend — VM Setup (Debian)"
echo "========================================"

# ── 1. System packages ──
info "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    curl wget git nginx certbot python3-certbot-nginx \
    postgresql postgresql-contrib \
    build-essential pkg-config \
    ufw

# ── 2. Install uv ──
info "Installing uv (astral.sh)..."
curl -LsSf https://astral.sh/uv/install.sh | bash
cp "$HOME/.local/bin/uv" /usr/local/bin/uv
cp "$HOME/.local/bin/uvx" /usr/local/bin/uvx
info "uv $(uv --version)"

# ── 3. Create app user ──
if id "$APP_USER" &>/dev/null; then
    info "User $APP_USER already exists"
else
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    info "Created system user: $APP_USER"
fi

# ── 4. Clone / pull repo ──
if [[ -d "$APP_DIR/.git" ]]; then
    warn "App dir already exists — pulling latest..."
    cd "$APP_DIR"
    sudo -u "$APP_USER" git pull --ff-only origin main
else
    sudo -u "$APP_USER" git clone --depth 1 --branch main "$REPO_URL" "$APP_DIR"
fi

# ── 5. Install Python dependencies ──
info "Installing Python dependencies with uv..."
sudo -u "$APP_USER" bash -c "cd $APP_DIR && uv sync --frozen --no-dev"

# ── 6. PostgreSQL setup ──
info "Configuring PostgreSQL..."
DB_PASS=$(openssl rand -hex 16)

sudo -u postgres psql -c "CREATE USER finance_ai WITH PASSWORD '${DB_PASS}';" 2>/dev/null || \
    sudo -u postgres psql -c "ALTER USER finance_ai WITH PASSWORD '${DB_PASS}';"
sudo -u postgres psql -c "CREATE DATABASE finance_ai OWNER finance_ai;" 2>/dev/null || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE finance_ai TO finance_ai;"

echo "Save this: DB_PASSWORD=${DB_PASS}" > "$APP_DIR/.db-password"

# ── 7. Create .env (production secrets) ──
SESSION_SECRET=$(openssl rand -hex 32)
if [[ ! -f "$APP_DIR/.env" ]]; then
    cat > "$APP_DIR/.env" <<EOF
FINANCE_AI_ENVIRONMENT=production
FINANCE_AI_DATABASE_URL=postgresql+psycopg://finance_ai:${DB_PASS}@localhost:5432/finance_ai
FINANCE_AI_LOG_LEVEL=INFO
FINANCE_AI_SESSION_LIMITER_SECRET=${SESSION_SECRET}
FINANCE_AI_ACCESS_LIFETIME_MINUTES=15
FINANCE_AI_RENEWABLE_IDLE_LIFETIME_DAYS=30
FINANCE_AI_SESSION_ABSOLUTE_LIFETIME_DAYS=90
FINANCE_AI_MAX_ACTIVE_SESSIONS_PER_USER=10
FINANCE_AI_TERMINAL_SESSION_RETENTION_DAYS=30
FINANCE_AI_MAX_REQUEST_BODY_BYTES=16384
FINANCE_AI_REGISTRATION_PER_MINUTE=10
FINANCE_AI_REGISTRATION_BURST_PER_10_SECONDS=3
FINANCE_AI_LOGIN_PER_MINUTE=10
FINANCE_AI_LOGIN_BURST_PER_10_SECONDS=5
FINANCE_AI_UNSAFE_PASSWORDS_PATH=${APP_DIR}/unsafe-passwords.txt
EOF
    info "Created .env with generated secrets"
    chmod 600 "$APP_DIR/.env"
else
    warn ".env already exists — leaving unchanged"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── 8. Run DB migrations ──
info "Running Alembic migrations..."
sudo -u "$APP_USER" bash -c "cd $APP_DIR && .venv/bin/alembic upgrade head"

# ── 9. Install systemd service ──
info "Installing systemd service..."
cp "$APP_DIR/deploy/finance-ai.service" /etc/systemd/system/finance-ai-backend.service
systemctl daemon-reload
systemctl enable finance-ai-backend
systemctl start finance-ai-backend
info "Service started: $(systemctl is-active finance-ai-backend)"

# ── 10. Install Nginx config ──
info "Configuring Nginx..."
mkdir -p /var/www/html
rm -f /etc/nginx/sites-enabled/default
ln -sf "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/finance-ai
ln -sf /etc/nginx/sites-available/finance-ai /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
info "Nginx configured"

# ── 11. Firewall ──
info "Configuring UFW firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow http
ufw allow https
ufw --force enable
info "UFW enabled (SSH, HTTP, HTTPS)"

# ── 12. Receipt storage ──
mkdir -p "$APP_DIR/receipts"
chown "$APP_USER:$APP_USER" "$APP_DIR/receipts"

# ── Final check ──
VM_IP=$(curl -4 -s ifconfig.me 2>/dev/null || echo "localhost")
echo ""
echo "========================================"
echo " Setup complete!"
echo "========================================"
echo ""
echo "  Backend:  http://${VM_IP}/health/live"
echo "  API docs: http://${VM_IP}/docs"
echo "  DB pass:  ${DB_PASS}  (saved in $APP_DIR/.db-password)"
echo "  Env:      $APP_DIR/.env"
echo ""
echo "  Logs:   journalctl -u finance-ai-backend -f"
echo "  Status: systemctl status finance-ai-backend"
echo ""
echo "  NEXT — add HTTPS with a domain:"
echo "    sudo certbot --nginx -d tu-dominio.com"
echo ""
echo "  Then add GitHub secrets for auto-deploy:"
echo "    AZURE_VM_HOST: ${VM_IP}"
echo "    AZURE_VM_USER: finance-admin"
echo "    AZURE_VM_SSH_KEY: (your private key)"
echo "========================================"