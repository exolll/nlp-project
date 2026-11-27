#!/usr/bin/env bash
# ProjectPilot one-shot bootstrap for Ubuntu 24.04 (Python 3.12 is the system python).
# Usage:  sudo bash bootstrap.sh <domain-or-ip> <mysql-strong-password>
# Re-runnable: it stops early (and tells you what to do) until .env has real keys.
set -euo pipefail

SITE="${1:-}"
DBPASS="${2:-}"
APP_DIR=/opt/projectpilot
REPO=https://github.com/exolll/nlp-project.git
BRANCH=slave1
VENV=$APP_DIR/.venv

log() { printf '\n=== %s ===\n' "$*"; }
die() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n "$SITE" ]] || die "usage: sudo bash bootstrap.sh <domain-or-ip> <mysql-password>"
[[ -n "$DBPASS" ]] || die "give a non-empty MySQL password (the app refuses an empty one)"

command -v python3.12 >/dev/null || die "python3.12 not found: use an Ubuntu 24.04 image (22.04 ships 3.10)"

log "1/9 system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv python3.12-dev nginx git mysql-server \
    curl build-essential libssl-dev ca-certificates >/dev/null

log "2/9 Node.js 20 (needed by Vite 6)"
if ! command -v node >/dev/null || [[ $(node -v | tr -d 'v' | cut -d. -f1) -lt 20 ]]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
    apt-get install -y -qq nodejs >/dev/null
fi
node -v

log "3/9 source code"
if [[ -d $APP_DIR/.git ]]; then
    git -C "$APP_DIR" fetch --all --quiet
    git -C "$APP_DIR" checkout "$BRANCH" --quiet
    git -C "$APP_DIR" pull --ff-only --quiet
else
    git clone --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
mkdir -p "$APP_DIR/data/objects" "$APP_DIR/data/chroma"

log "4/9 python environment"
[[ -d $VENV ]] || python3.12 -m venv "$VENV"
"$VENV/bin/pip" install -q -U pip setuptools wheel
"$VENV/bin/pip" install -q --no-build-isolation -e "$APP_DIR/backend[test]"
"$VENV/bin/pip" install -q "chromadb>=1.5,<2"
"$VENV/bin/python" -c "import fastapi, uvicorn, chromadb, sqlalchemy; print('deps ok')"

log "5/9 mysql database"
mysql -e "CREATE DATABASE IF NOT EXISTS projectpilot_search CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
mysql -e "CREATE USER IF NOT EXISTS 'pp_user'@'127.0.0.1' IDENTIFIED BY '${DBPASS}';"
mysql -e "ALTER USER 'pp_user'@'127.0.0.1' IDENTIFIED BY '${DBPASS}';"
mysql -e "GRANT ALL PRIVILEGES ON projectpilot_search.* TO 'pp_user'@'127.0.0.1'; FLUSH PRIVILEGES;"

log "6/9 .env"
if [[ ! -f $APP_DIR/.env ]]; then
    cat > "$APP_DIR/.env" <<EOF
ENVIRONMENT=development
STORAGE_BACKEND=local
LOCAL_STORAGE_DIRECTORY=$APP_DIR/data/objects
CHROMA_PATH=$APP_DIR/data/chroma
CORS_ORIGINS=["http://$SITE"]

MAX_FILE_BYTES=10000000
MAX_REQUEST_BYTES=11534336
EXTRACTION_TIMEOUT_SECONDS=90
EXTRACTION_MAX_INPUT_TOKENS=60000
EXTRACTION_MAX_OUTPUT_TOKENS=8000
EXTRACTION_MAX_CALLS=20
MAX_TASK_EXTRACTIONS=10
MAX_REQUIREMENT_REVISIONS=50

DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
SILICONFLOW_API_KEY=
EMBEDDING_ENABLED=true
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSION=1024

GITHUB_TOKEN=

MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=projectpilot_search
MYSQL_USER=pp_user
MYSQL_PASSWORD=${DBPASS}
EOF
    chmod 600 "$APP_DIR/.env"
fi

if grep -Eq '^(DEEPSEEK_API_KEY|SILICONFLOW_API_KEY|GITHUB_TOKEN)=$' "$APP_DIR/.env"; then
    log "STOP: fill the three keys in $APP_DIR/.env, then run this script again"
    grep -E '^(DEEPSEEK_API_KEY|SILICONFLOW_API_KEY|GITHUB_TOKEN)=' "$APP_DIR/.env" | sed 's/=$/=<fill me>/'
    exit 0
fi

log "7/9 database schema"
( cd "$APP_DIR/backend" && PROJECTPILOT_ALLOW_MIGRATIONS=1 "$VENV/bin/python" -m alembic upgrade head )

log "8/9 backend service"
cat > /etc/systemd/system/projectpilot.service <<EOF
[Unit]
Description=ProjectPilot API
After=network.target mysql.service

[Service]
User=www-data
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$APP_DIR/.env
ExecStart=$VENV/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
chown -R www-data:www-data "$APP_DIR/data"
usermod -aG www-data www-data 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now projectpilot
sleep 3
systemctl is-active --quiet projectpilot || { journalctl -u projectpilot -n 30 --no-pager; die "backend failed to start"; }

log "9/9 frontend build + nginx"
( cd "$APP_DIR/frontend" && npm ci --silent && npm run build --silent )
mkdir -p /var/www/projectpilot
rm -rf /var/www/projectpilot/*
cp -r "$APP_DIR/frontend/dist/." /var/www/projectpilot/
chown -R www-data:www-data /var/www/projectpilot

cat > /etc/nginx/sites-available/projectpilot <<EOF
server {
    listen 80;
    server_name $SITE;
    client_max_body_size 12m;

    root /var/www/projectpilot;
    index index.html;
    location / { try_files \$uri /index.html; }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 180s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/projectpilot /etc/nginx/sites-enabled/projectpilot
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

log "verify"
curl -fsS "http://127.0.0.1:8000/api/v1/config" >/dev/null && echo "backend config ok"
curl -fsS -o /dev/null -w 'frontend http %{http_code}\n' "http://127.0.0.1/"
curl -fsS -o /dev/null -w 'api via nginx http %{http_code}\n' "http://127.0.0.1/api/v1/config"

printf '\nDONE. Open http://%s/\n' "$SITE"
echo "Logs:      journalctl -u projectpilot -f"
echo "DB backup: mysqldump -u pp_user -p projectpilot_search > backup.sql"
echo "Data dir:  $APP_DIR/data (add your own snapshot policy)"
