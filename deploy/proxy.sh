#!/usr/bin/env bash
# Сервер Ubuntu перед АвтоВес: домен, HTTPS и выход в интернет для приложения водителя и кабинета логиста.
#   sudo bash deploy/proxy.sh ves.example.ru 192.168.1.50:8790
# Первый параметр — домен (должен указывать на внешний адрес этого сервера, порты 80 и 443 открыты).
# Второй — адрес компьютера с АвтоВес в локальной сети и его ВНЕШНИЙ порт (server.public_port, по умолчанию 8790).
# Основной порт АвтоВес (рабочее место оператора, светофоры, настройки) сюда указывать нельзя.
set -euo pipefail

DOMAIN="${1:-}"
UPSTREAM="${2:-}"
if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo." >&2; exit 1; fi
if [ -z "$DOMAIN" ] || ! [[ "$UPSTREAM" =~ ^[0-9A-Za-z.-]+:[0-9]+$ ]]; then
  echo "Использование: sudo bash deploy/proxy.sh ДОМЕН АДРЕС_АВТОВЕС:ВНЕШНИЙ_ПОРТ" >&2; exit 1
fi

case "$DOMAIN" in
  ваш-домен*|ves.example.ru|*example*)
    echo "«$DOMAIN» — это пример из инструкции. Укажите свой настоящий домен." >&2; exit 1;;
esac
# Домен на кириллице (например, в зоне .рф) переводим в запись, понятную сертификату (Punycode)
if printf '%s' "$DOMAIN" | LC_ALL=C grep -q '[^ -~]'; then
  DOMAIN="$(python3 -c 'import sys; print(sys.argv[1].encode("idna").decode())' "$DOMAIN")"
  echo "Домен в записи Punycode: $DOMAIN"
fi

apt-get update -y
apt-get install -y nginx certbot python3-certbot-nginx curl

if ! curl -fsS -m 5 "http://$UPSTREAM/api/ping" >/dev/null; then
  echo "АвтоВес не отвечает по адресу http://$UPSTREAM. Проверьте адрес, внешний порт и брандмауэр Windows." >&2; exit 1
fi
if curl -fsS -m 5 -o /dev/null "http://$UPSTREAM/api/op/status" 2>/dev/null; then
  echo "Это основной порт АвтоВес. Укажите внешний порт (public_port), иначе управление весовой окажется в интернете." >&2; exit 1
fi

cat > /etc/nginx/conf.d/avtoves-limits.conf <<EOF
limit_req_zone \$binary_remote_addr zone=avtoves_login:10m rate=20r/m;
EOF
cat > /etc/nginx/sites-available/avtoves <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 1m;

    location ~ ^/api/(app|logist)/login\$ {
        limit_req zone=avtoves_login burst=10 nodelay;
        proxy_pass http://$UPSTREAM;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    location / {
        proxy_pass http://$UPSTREAM;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/avtoves /etc/nginx/sites-enabled/avtoves
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect

echo
echo "Готово. Кабинет логиста: https://$DOMAIN/logist"
echo "Адрес сервера для приложения водителя: $DOMAIN"
