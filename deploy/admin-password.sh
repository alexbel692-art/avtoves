#!/usr/bin/env bash
# Новый пароль администратора кабинета АвтоВес (если забыли): sudo bash deploy/admin-password.sh
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo." >&2; exit 1; fi
sudo -u avtoves /opt/avtoves/venv/bin/python /opt/avtoves/main.py --config /var/lib/avtoves/config.yaml --reset-admin
