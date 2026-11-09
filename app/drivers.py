"""Водители: PIN-коды, вход с телефона, защита от перебора."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path

TOKEN_DAYS = 30


class DriverAuth:
    def __init__(self, data_dir: str | Path):
        key_file = Path(data_dir) / "driver.key"
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32), encoding="ascii")
        self.key = bytes.fromhex(key_file.read_text(encoding="ascii").strip())
        self.fails: dict[str, list[float]] = {}

    # PIN хранится как HMAC — по нему можно найти водителя, но нельзя восстановить PIN
    def pin_hash(self, pin: str) -> str:
        return hmac.new(self.key, b"pin:" + pin.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def valid_pin(pin: str) -> bool:
        return bool(re.fullmatch(r"\d{4,8}", pin or ""))

    # Токен входа: id.срок.подпись — переживает перезапуск службы, отзывается отключением водителя
    def token(self, driver_id: int) -> str:
        exp = int(time.time()) + TOKEN_DAYS * 86400
        body = f"{driver_id}.{exp}"
        sig = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{body}.{sig}"

    def check(self, token: str | None) -> int | None:
        try:
            did, exp, sig = (token or "").split(".")
            body = f"{did}.{exp}"
            good = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:32]
            if hmac.compare_digest(sig, good) and int(exp) > time.time():
                return int(did)
        except ValueError:
            pass
        return None

    # Перебор PIN: 5 ошибок за 5 минут с одного адреса — блокировка на 5 минут
    def blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self.fails.get(ip, []) if now - t < 300]
        self.fails[ip] = recent
        return len(recent) >= 5

    def failed(self, ip: str):
        self.fails.setdefault(ip, []).append(time.time())

    def ok(self, ip: str):
        self.fails.pop(ip, None)
