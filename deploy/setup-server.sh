#!/usr/bin/env bash
# Развёртывание ROZMART Mini App на чистом Debian 12.
#
# Ставит PostgreSQL, приложение, nginx и регламентную синхронизацию с REGOS.
# Повторный запуск безопасен: существующие база, пароли и конфиги не затираются.
#
# Запускать ПОСЛЕ harden-server.sh — там открываются порты 80 и 443.
#
#   bash setup-server.sh
#
# Переменные (спросит, если не заданы):
#   REGOS_KEY              ключ локальной интеграции REGOS
#   BOT_TOKEN              токен бота из BotFather
#   REGOS_STOCK_ID         склад, по умолчанию 5 (СКЛАД RDB)
#   REGOS_PRICE_TYPE_ID    тип цены этого же магазина, по умолчанию 5
#   REGOS_ITEM_GROUPS      группы номенклатуры через запятую, пусто — весь каталог
#   NOTIFY_BOT_TOKEN       токен информационного бота (сообщения о заказе)
#   PUBLIC_URL             адрес сайта, например https://shop.example.com
set -euo pipefail

APP_USER=rozmart
APP_DIR=/opt/rozmart
ENV_FILE=/etc/rozmart/env
# Фото товаров, загруженные в панели и скачанные из REGOS. Вне каталога
# приложения: выкладка кода его перезаписывает, а фото должны пережить её
PHOTO_DIR=/var/lib/rozmart/img
REPO="${REPO:-https://github.com/davronbekr016-del/rozmart.git}"
DB_NAME=rozmart
DB_USER=rozmart

die() { echo "ОШИБКА: $*" >&2; exit 1; }
say() { echo; echo "── $*"; }

[ "$(id -u)" -eq 0 ] || die "нужен root"

# ---------------------------------------------------------------------------
say "Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-dev build-essential \
  postgresql postgresql-contrib \
  nginx git curl

# ---------------------------------------------------------------------------
say "База данных"
systemctl enable --now postgresql

# Пароль генерируется на сервере и никуда не пересылается. Если база уже
# заведена — пароль не меняем, иначе отвалится работающее приложение.
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1; then
  echo "роль ${DB_USER} уже есть, пароль не трогаю"
  [ -f "$ENV_FILE" ] || die "роль есть, а $ENV_FILE нет — пароль утерян. Задайте его вручную."
else
  # Роли нет, но файл настроек остался от прошлой установки: в нём лежит
  # пароль от роли, которой больше не существует. Молча создав новую роль,
  # мы получили бы приложение, которое не может подключиться к базе.
  if [ -f "$ENV_FILE" ]; then
    die "роли ${DB_USER} нет, но $ENV_FILE остался от прошлой установки.
     Удалите его (rm $ENV_FILE) и запустите скрипт заново — он спросит
     токен бота и ключ REGOS и пропишет новый пароль базы."
  fi
  DB_PASS=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
  sudo -u postgres psql -qc "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}'"
  echo "роль ${DB_USER} создана"
fi

sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
  || sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"

# ---------------------------------------------------------------------------
say "Пользователь и код"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# фото товаров: на первом запуске — те, что пришли с кодом в static/img
mkdir -p "$PHOTO_DIR"
cp -n "$APP_DIR"/static/img/* "$PHOTO_DIR"/ 2>/dev/null || true
chown -R "$APP_USER:$APP_USER" "$(dirname "$PHOTO_DIR")"

# ---------------------------------------------------------------------------
say "Настройки"
mkdir -p "$(dirname "$ENV_FILE")"

if [ ! -f "$ENV_FILE" ]; then
  [ -n "${BOT_TOKEN:-}" ]  || read -rp "Токен бота из BotFather: " BOT_TOKEN
  [ -n "${REGOS_KEY:-}" ]  || read -rp "Ключ интеграции REGOS: " REGOS_KEY

  cat > "$ENV_FILE" <<EOF
# Секреты приложения. Файл читает только systemd, права 600.
DATABASE_URL=postgresql+psycopg://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}
BOT_TOKEN=${BOT_TOKEN}

REGOS_KEY=${REGOS_KEY}
REGOS_STOCK_ID=${REGOS_STOCK_ID:-5}
REGOS_PRICE_TYPE_ID=${REGOS_PRICE_TYPE_ID:-5}
REGOS_ITEM_GROUPS=${REGOS_ITEM_GROUPS:-}

# Выгрузка заказов включится, когда в REGOS появится источник в справочнике
# DeliveryFrom (сейчас там только «Uzum tezkor»). Его id сюда.
REGOS_ORDER_FROM_ID=0

# Информационный бот: сообщения покупателю о его заказе (app/notify.py).
# Это ВТОРОЙ бот, не тот, через который открывается магазин: у них разные
# задачи, и один токен на оба означал бы, что смена одного ломает другое.
NOTIFY_BOT_TOKEN=${NOTIFY_BOT_TOKEN:-}
# Секрет, которым Telegram подписывает каждое обновление. Без него приложение
# обновления отвергает: открытый адрес позволил бы кому угодно слать
# сообщения от имени нашего бота незнакомым людям.
NOTIFY_WEBHOOK_SECRET=${NOTIFY_WEBHOOK_SECRET:-$(openssl rand -hex 24)}
# Адрес сайта — по нему регистрируется вебхук (python -m scripts.setup_bot).
PUBLIC_URL=${PUBLIC_URL:-}
# Куда панель сохраняет фото товаров (см. PHOTO_DIR выше и nginx ниже).
PHOTO_DIR=${PHOTO_DIR}
EOF
  echo "создан $ENV_FILE"
else
  echo "$ENV_FILE уже есть, не трогаю"
fi
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

# ---------------------------------------------------------------------------
say "Служба приложения"
cat > /etc/systemd/system/rozmart.service <<EOF
[Unit]
Description=ROZMART Mini App
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

# Приложению нужен только свой каталог: читать остальную файловую систему
# и лезть в /home ему незачем
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=${APP_DIR} $(dirname ${PHOTO_DIR})

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------------------
say "Регламентная синхронизация каталога"
# Инкрементальной выгрузки REGOS на текущем тарифе не даёт (фильтр по
# last_update отклоняется с ошибкой 7501), поэтому каталог проходится целиком.
# 8050 позиций — это около полутора минут при лимите 2 запроса в секунду,
# поэтому раз в полчаса, а не раз в минуту.
cat > /etc/systemd/system/rozmart-sync.service <<EOF
[Unit]
Description=Синхронизация каталога ROZMART с REGOS
After=network.target rozmart.service

[Service]
Type=oneshot
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python -m scripts.sync_regos
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=${APP_DIR} $(dirname ${PHOTO_DIR})
EOF

cat > /etc/systemd/system/rozmart-sync.timer <<EOF
[Unit]
Description=Синхронизация каталога ROZMART каждые 30 минут

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
# разброс, чтобы после перезагрузки не бить в REGOS одновременно со стартом
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now rozmart.service
systemctl enable --now rozmart-sync.timer

# ---------------------------------------------------------------------------
say "nginx"
# Пока домена нет — отдаём по IP на 80-м. Когда домен появится:
#   sed -i "s/server_name _;/server_name ваш.домен;/" /etc/nginx/sites-available/rozmart
#   apt-get install -y certbot python3-certbot-nginx
#   certbot --nginx -d ваш.домен
# certbot сам допишет TLS и редирект с 80-го.
cat > /etc/nginx/sites-available/rozmart <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name _;

    # Telegram Mini App открывается только по HTTPS. По IP работать не будет —
    # это временная конфигурация до появления домена.

    client_max_body_size 4m;

    # фото товаров — из каталога фото, а не из кода (PHOTO_DIR)
    location /static/img/ {
        alias /var/lib/rozmart/img/;
        expires 7d;
        access_log off;
    }

    location /static/ {
        alias /opt/rozmart/static/;
        expires 7d;
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/rozmart /etc/nginx/sites-enabled/rozmart
rm -f /etc/nginx/sites-enabled/default
nginx -t || die "конфигурация nginx невалидна"
systemctl enable --now nginx
systemctl reload nginx

# ---------------------------------------------------------------------------
say "Первая синхронизация каталога"
systemctl start rozmart-sync.service || true
journalctl -u rozmart-sync.service -n 8 --no-pager | sed 's/^/  /'

say "Итог"
for unit in postgresql rozmart nginx rozmart-sync.timer; do
  printf "%-22s %s\n" "$unit" "$(systemctl is-active $unit)"
done
echo
curl -fsS localhost/health && echo " ← приложение отвечает"
cat <<MSG

Дальше:
  1. Когда появится домен — направить его A-записью на этот сервер, затем:
       sed -i 's/server_name _;/server_name ВАШ.ДОМЕН;/' /etc/nginx/sites-available/rozmart
       apt-get install -y certbot python3-certbot-nginx
       certbot --nginx -d ВАШ.ДОМЕН
     До этого Telegram Mini App не откроется: он требует HTTPS.
  2. Указать адрес Mini App в BotFather.
  3. Каталог синхронизируется сам каждые 30 минут:
       systemctl list-timers rozmart-sync.timer
       journalctl -u rozmart-sync.service -f
MSG
