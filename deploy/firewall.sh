#!/usr/bin/env bash
# Брандмауэр сервера АвтоВес (ufw):
#   из интернета — только 80 и 443 (nginx с HTTPS);
#   из локальной сети — всё (SSH, рабочее место оператора 8780, поиск Moxa и камер, ответы устройств).
# Отключить: sudo ufw disable
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo." >&2; exit 1; fi
apt-get install -y ufw >/dev/null
LANS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
for net in $LANS; do
  ufw allow from "$net" comment 'Локальная сеть'
done
ufw allow 80/tcp comment 'HTTP: выпуск сертификата и переход на HTTPS'
ufw allow 443/tcp comment 'HTTPS: приложение водителя и кабинеты'
ufw --force enable
ufw status verbose
