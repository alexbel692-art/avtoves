"""Чтение веса с индикатора CAS CI-200A через Moxa NPort (TCP или COM-порт).

Индикатор должен быть в режиме непрерывной передачи (поток).
Типичная посылка CAS:  ST,GS,<ID><LAMP>+  1234.5 kg\r\n
  ST/US/OL — стабильно / нестабильно / перегруз
  GS/NT    — брутто / нетто
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("scale")

_UNIT_RE = re.compile(rb"(kg|KG|Kg|lb|LB|t|T|g)\s*$")
_NUM_RE = re.compile(rb"([+-])?\s*(\d+(?:[.,]\d+)?)")


@dataclass
class Reading:
    weight: float | None        # кг
    stable: bool                # флаг индикатора ST
    overload: bool
    net: bool
    raw: str
    ts: float = field(default_factory=time.time)


def parse_cas(line: bytes, data_len: int = 8) -> Reading | None:
    """Разбор строки формата CAS CI. Возвращает None, если строка не похожа на вес."""
    s = line.strip(b"\r\n\x00")
    if len(s) < 6:
        return None
    raw = s.decode("latin-1", "replace")
    head = s[:6].upper()
    status = head[:2]
    if status not in (b"ST", b"US", b"OL"):
        # Иногда поток начинается с середины посылки — ищем заголовок дальше
        m = re.search(rb"(ST|US|OL),(GS|NT),", s.upper())
        if not m:
            return None
        s = s[m.start():]
        head = s[:6].upper()
        status = head[:2]
    net = head[3:5] == b"NT"
    if status == b"OL":
        return Reading(None, False, True, net, raw)

    m = _UNIT_RE.search(s)
    unit = m.group(1).lower() if m else b"kg"
    body = s[: m.start()] if m else s
    body = body.rstrip()
    # Поле веса — последние data_len байт перед единицами
    field_ = body[-data_len:] if len(body) > 6 + data_len else body[6:]
    w = _to_number(field_)
    if w is None:
        w = _to_number(body[6:])
    if w is None:
        return None
    if unit == b"t":
        w *= 1000
    elif unit == b"g":
        w /= 1000
    return Reading(round(w, 3), status == b"ST", False, net, raw)


def parse_generic(line: bytes, data_len: int = 8) -> Reading | None:
    s = line.strip(b"\r\n\x00")
    nums = list(_NUM_RE.finditer(s))
    if not nums:
        return None
    w = _to_number(nums[-1].group(0))
    if w is None:
        return None
    return Reading(w, True, False, False, s.decode("latin-1", "replace"))


def _to_number(b: bytes) -> float | None:
    """Последнее число в поле (перед ним могут стоять байты ID и ламп)."""
    ms = list(_NUM_RE.finditer(b))
    if not ms:
        return None
    m = ms[-1]
    digits = m.group(2).replace(b",", b".")
    try:
        v = float(digits)
    except ValueError:
        return None
    return -v if m.group(1) == b"-" else v


class _Transport:
    """TCP-сокет к Moxa NPort (TCP Server) или COM-порт (Real COM)."""

    def __init__(self, url: str):
        self.url = urlparse(url)
        self.sock = None
        self.ser = None

    def open(self):
        if self.url.scheme == "tcp":
            self.sock = socket.create_connection((self.url.hostname, self.url.port or 4001), timeout=5)
            self.sock.settimeout(1.0)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        elif self.url.scheme == "serial":
            import serial  # pyserial
            q = parse_qs(self.url.query)
            port = self.url.netloc or self.url.path.lstrip("/")
            self.ser = serial.Serial(
                port, baudrate=int(q.get("baud", ["9600"])[0]),
                bytesize=int(q.get("bits", ["8"])[0]),
                parity=q.get("parity", ["N"])[0].upper(),
                stopbits=float(q.get("stop", ["1"])[0]), timeout=1.0)
        else:
            raise ValueError(f"Неизвестная схема подключения весов: {self.url.scheme}")

    def read(self) -> bytes:
        if self.sock:
            try:
                data = self.sock.recv(1024)
            except socket.timeout:
                return b""
            if not data:
                raise ConnectionError("NPort закрыл соединение")
            return data
        return self.ser.read(self.ser.in_waiting or 1)

    def close(self):
        for o in (self.sock, self.ser):
            try:
                o and o.close()
            except Exception:
                pass
        self.sock = self.ser = None


class Scale:
    """Фоновый поток: читает вес, считает стабильность, хранит последнее значение."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.parser = parse_cas if cfg.get("parser", "cas") == "cas" else parse_generic
        self.data_len = int(cfg.get("data_len", 8))
        self.lock = threading.Lock()
        self.last: Reading | None = None
        self.history: deque[tuple[float, float]] = deque(maxlen=400)
        self.raw_lines: deque[str] = deque(maxlen=30)
        self.connected = False
        self.error = ""
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, name="scale", daemon=True)

    def start(self):
        self._thr.start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            tr = _Transport(self.cfg["connection"])
            try:
                tr.open()
                log.info("Весы: подключено %s", self.cfg["connection"])
                self.error = ""
                backoff = 1
                buf = b""
                while not self._stop.is_set():
                    chunk = tr.read()
                    if chunk:
                        buf += chunk
                        *lines, buf = re.split(rb"\r\n|\n|\r", buf)
                        for ln in lines:
                            self._handle(ln)
                        if len(buf) > 4096:
                            buf = b""
                    self.connected = self.last is not None and time.time() - self.last.ts < self.cfg["timeout_sec"]
            except Exception as e:  # noqa: BLE001
                self.connected = False
                self.error = str(e)
                log.warning("Весы: %s, повтор через %s с", e, backoff)
            finally:
                tr.close()
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 15)

    def _handle(self, line: bytes):
        if not line.strip():
            return
        r = self.parser(line, self.data_len)
        with self.lock:
            self.raw_lines.append(line.decode("latin-1", "replace"))
            if r is None:
                return
            self.last = r
            if r.weight is not None:
                self.history.append((r.ts, r.weight))

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            r = self.last
            fresh = r is not None and now - r.ts < self.cfg["timeout_sec"]
            weight = r.weight if fresh else None
            stable = fresh and self._is_stable(now, r)
            return {
                "connected": fresh,
                "weight": weight,
                "stable": stable,
                "indicator_stable": bool(r and r.stable),
                "overload": bool(r and r.overload),
                "net": bool(r and r.net),
                "error": "" if fresh else (self.error or "нет данных от индикатора"),
                "raw": list(self.raw_lines)[-5:],
            }

    def _is_stable(self, now: float, r: Reading) -> bool:
        if r.weight is None:
            return False
        if self.cfg.get("use_indicator_stable", True) and not r.stable:
            return False
        win = float(self.cfg["stable_time"])
        pts = [w for t, w in self.history if now - t <= win]
        oldest = next((t for t, _ in self.history if now - t <= win), now)
        if len(pts) < 3 or now - oldest < win * 0.8:
            return False
        return max(pts) - min(pts) <= float(self.cfg["stable_delta"])
