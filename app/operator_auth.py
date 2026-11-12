"""Пароль оператора: вход на рабочее место АвтоВес.

Вход запоминается подписанной cookie на 30 дней (переживает перезапуск службы).
Смена пароля сразу выкидывает все входы: в подпись входит отпечаток пароля.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

COOKIE = "avtoves_op"
DAYS = 30

# Без пароля оператора доступны только эти адреса
PUBLIC_EXACT = {"/login", "/api/op/login", "/api/op/status", "/api/ping", "/driver", "/logist"}
# /api/app/ и /api/logist/ защищены своими логином и паролем
PUBLIC_PREFIX = ("/static/", "/api/driver/", "/api/1c/", "/api/app/", "/api/logist/")


class OperatorAuth:
    def __init__(self, data_dir: str | Path):
        f = Path(data_dir) / "operator.key"
        if not f.exists():
            f.write_text(secrets.token_hex(32), encoding="ascii")
        self.key = bytes.fromhex(f.read_text(encoding="ascii").strip())
        self.fails: dict[str, list[float]] = {}

    @staticmethod
    def _pwver(stored: str) -> str:
        return hashlib.sha256(stored.encode()).hexdigest()[:12]

    def cookie(self, stored_hash: str) -> str:
        body = f"{int(time.time()) + DAYS * 86400}.{self._pwver(stored_hash)}"
        return body + "." + hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:32]

    def valid(self, value: str | None, stored_hash: str) -> bool:
        try:
            exp, ver, sig = (value or "").split(".")
        except ValueError:
            return False
        body = f"{exp}.{ver}"
        good = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()[:32]
        return (hmac.compare_digest(sig, good) and int(exp) > time.time()
                and hmac.compare_digest(ver, self._pwver(stored_hash)))

    def blocked(self, ip: str) -> bool:
        now = time.time()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < 300]
        return len(self.fails[ip]) >= 5

    def failed(self, ip: str):
        self.fails.setdefault(ip, []).append(time.time())


class OperatorGate(BaseHTTPMiddleware):
    """Закрывает рабочее место оператора паролем (если он задан в настройках)."""

    def __init__(self, app, ctx, auth: OperatorAuth, driver_token_ok):
        super().__init__(app)
        self.ctx, self.auth, self.driver_token_ok = ctx, auth, driver_token_ok

    async def dispatch(self, request: Request, call_next):
        stored = self.ctx.cfg["server"].get("operator_password")
        path = request.url.path
        if not stored or path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX):
            return await call_next(request)
        if self.auth.valid(request.cookies.get(COOKIE), stored):
            return await call_next(request)
        # фото взвешивания водитель видит по своему токену
        if path.startswith("/snapshots/") and self.driver_token_ok(request.query_params.get("t")):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"error": "нужен вход оператора", "need_op_login": True}, 401)
        return RedirectResponse("/login", 302)
