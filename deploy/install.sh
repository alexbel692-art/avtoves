#!/usr/bin/env bash
# АвтоВес 2 на Ubuntu 22.04/24.04: сервер программы, база данных и выход в интернет на одном сервере.
#   sudo bash deploy/install.sh ves.example.ru     с доменом и HTTPS для приложения водителя и кабинетов
#   sudo bash deploy/install.sh                    без домена: только локальная сеть
# Сервер должен быть в одной сети с весами (Moxa) и камерами.
# Повторный запуск обновляет программу; настройки и база в /var/lib/avtoves не трогаются.
set -euo pipefail

DOMAIN="${1:-}"
APP=/opt/avtoves
DATA=/var/lib/avtoves
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo." >&2; exit 1; fi

apt-get update -y
apt-get install -y python3 python3-venv rsync curl libgl1 libglib2.0-0

id avtoves >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin avtoves
mkdir -p "$APP" "$DATA"
rsync -a --delete --exclude '__pycache__' "$SRC/main.py" "$SRC/app" "$SRC/requirements.txt" "$APP/"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"

if [ ! -f "$DATA/config.yaml" ]; then
  cat > "$DATA/config.yaml" <<EOF
# Остальные настройки (весы, камеры, светофоры, газ) задаются в программе оператора: вкладка «Настройки».
data_dir: data
server:
  host: 0.0.0.0
  port: 8780          # программа оператора, только локальная сеть
  public_port: 8790   # приложение водителя и кабинеты, в интернет через nginx
EOF
fi
chown -R avtoves:avtoves "$DATA"
chmod 750 "$DATA"

cat > /etc/systemd/system/avtoves.service <<EOF
[Unit]
Description=AvtoVes server
After=network-online.target
Wants=network-online.target

[Service]
User=avtoves
Group=avtoves
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python main.py --config $DATA/config.yaml
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable avtoves
systemctl restart avtoves

for _ in $(seq 1 30); do
  curl -fsS -m 2 http://127.0.0.1:8790/api/ping >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS -m 2 http://127.0.0.1:8790/api/ping >/dev/null; then
  echo "Сервер не запустился. Журнал: journalctl -u avtoves -n 50" >&2; exit 1
fi

if [ -n "$DOMAIN" ]; then
  bash "$SRC/deploy/proxy.sh" "$DOMAIN" "127.0.0.1:8790"
fi

IP="$(hostname -I | awk '{print $1}')"
echo
echo "АвтоВес запущен."
echo "Программа оператора на компьютерах: адрес сервера http://$IP:8780"
[ -n "$DOMAIN" ] && echo "Приложение водителя и кабинеты: https://$DOMAIN (кабинет: https://$DOMAIN/logist)"
echo "В интернет пробрасывайте только порты 80 и 443. Порты 8780 и 8790 наружу не открывайте."
