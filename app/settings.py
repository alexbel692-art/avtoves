"""Настройки из программы: форма ⇄ config.yaml, проверка значений, пароль, проверка связи с оборудованием."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from .config import DEFAULTS, _merge

HEADER = """\
# ============================================================
#  АвтоВес — настройки
#  Меняются в программе: АвтоВес → вкладка «Настройки».
#  Файл можно править и вручную — затем «Перезапустить АвтоВес».
# ============================================================
"""

RELAYS = ["a_red", "a_green", "b_red", "b_green", "barrier_a", "barrier_b"]
INPUTS = ["ir_a", "ir_b"]


# ------------------------------------------------------------------ файл
def load_raw(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_raw(path: str | Path, raw: dict):
    path = Path(path)
    text = HEADER + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False)
    yaml.safe_load(text)                      # что записываем — должно читаться
    if path.exists():
        bak = path.with_suffix(".yaml.bak")
        bak.write_bytes(path.read_bytes())
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ пароль настроек
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(calc, h)


class Sessions:
    """Входы в настройки (в памяти, 12 часов)."""

    def __init__(self, ttl: float = 12 * 3600):
        self.ttl = ttl
        self.tokens: dict[str, float] = {}

    def new(self) -> str:
        t = secrets.token_urlsafe(24)
        self.tokens[t] = time.time() + self.ttl
        return t

    def valid(self, t: str | None) -> bool:
        exp = self.tokens.get(t or "")
        if not exp:
            return False
        if exp < time.time():
            self.tokens.pop(t, None)
            return False
        return True

    def clear(self):
        self.tokens.clear()


# ------------------------------------------------------------------ config ⇄ форма
def to_form(raw: dict) -> dict:
    c = _merge(DEFAULTS, raw)
    sc, io, w, an, sv = c["scale"], c["io"], c["weighing"], c["anpr"], c["server"]
    u = urlparse(sc.get("connection", ""))
    if u.scheme == "serial":
        q = parse_qs(u.query)
        conn = {"type": "serial", "host": "", "port": 4001,
                "com": u.netloc or u.path.lstrip("/"), "baud": int(q.get("baud", ["9600"])[0])}
    else:
        conn = {"type": "tcp", "host": u.hostname or "", "port": u.port or 4001, "com": "COM3", "baud": 9600}
    relays = io.get("relays") or {}
    inputs = io.get("inputs") or {}
    return {
        "simulate": bool(c.get("simulate")),
        "scale": {
            **conn,
            "parser": sc.get("parser", "cas"),
            "min_weight": sc.get("min_weight"), "empty_weight": sc.get("empty_weight"),
            "stable_time": sc.get("stable_time"), "stable_delta": sc.get("stable_delta"),
            "use_indicator_stable": bool(sc.get("use_indicator_stable", True)),
            "timeout_sec": sc.get("timeout_sec"),
        },
        "io": {
            "enabled": bool(io.get("enabled")), "host": io.get("host", ""), "port": io.get("port", 502),
            "unit_id": io.get("unit_id", 1), "coil_base": io.get("coil_base", 0), "di_base": io.get("di_base", 0),
            "relays": {k: relays.get(k) for k in RELAYS},
            "inputs": {k: inputs.get(k) for k in INPUTS},
            "ir_active_level": io.get("ir_active_level", 1),
            "barrier_pulse_sec": io.get("barrier_pulse_sec", 1.0),
        },
        "weighing": {k: w.get(k) for k in ("mode", "auto_fix", "clear_time", "trip_max_hours", "plate_window_sec")},
        "anpr": {"min_confidence": an.get("min_confidence"), "interval_ms": an.get("interval_ms")},
        "cameras": [{"id": cam.get("id", f"cam{i + 1}"), "name": cam.get("name", ""), "side": cam.get("side"),
                     "url": cam.get("url", ""), "anpr": bool(cam.get("anpr", True))}
                    for i, cam in enumerate(c.get("cameras") or [])],
        "server": {"port": sv.get("port"), "api_token": sv.get("api_token", ""),
                   "protected": bool(sv.get("settings_password"))},
    }


class FormError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def _host(v, field, errors, required=True) -> str:
    v = str(v or "").strip()
    if not v:
        if required:
            errors[field] = "укажите адрес"
        return ""
    try:
        ipaddress.ip_address(v)
        return v
    except ValueError:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", v):
            return v
    errors[field] = "неверный адрес"
    return v


def _num(v, field, errors, lo, hi, kind=float):
    try:
        x = kind(str(v).replace(",", "."))
    except (TypeError, ValueError):
        errors[field] = "введите число"
        return lo
    if not lo <= x <= hi:
        errors[field] = f"от {lo} до {hi}"
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    return x


def _relay(v, field, errors):
    if v in (None, "", "null", "-"):
        return None
    return _num(v, field, errors, 0, 31, int)


def apply_form(raw: dict, f: dict) -> dict:
    """Новый config (dict) из формы. Неизвестные ключи исходного файла сохраняются."""
    e: dict[str, str] = {}
    out = copy.deepcopy(raw)
    for k in ("scale", "io", "weighing", "anpr", "server"):
        out.setdefault(k, {})
    out["simulate"] = bool(f.get("simulate"))

    s = f.get("scale", {})
    if s.get("type") == "serial":
        com = str(s.get("com", "")).strip().upper()
        if not re.fullmatch(r"COM\d{1,3}", com):
            e["scale.com"] = "например COM3"
        baud = _num(s.get("baud"), "scale.baud", e, 1200, 115200, int)
        out["scale"]["connection"] = f"serial://{com}?baud={baud}"
    else:
        host = _host(s.get("host"), "scale.host", e, required=not out["simulate"]) or "192.168.1.50"
        port = _num(s.get("port"), "scale.port", e, 1, 65535, int)
        out["scale"]["connection"] = f"tcp://{host}:{port}"
    out["scale"]["parser"] = "generic" if s.get("parser") == "generic" else "cas"
    out["scale"]["min_weight"] = _num(s.get("min_weight"), "scale.min_weight", e, 1, 100000)
    out["scale"]["empty_weight"] = _num(s.get("empty_weight"), "scale.empty_weight", e, 0, 100000)
    if "scale.min_weight" not in e and "scale.empty_weight" not in e and \
            out["scale"]["empty_weight"] >= out["scale"]["min_weight"]:
        e["scale.empty_weight"] = "должен быть меньше порога заезда"
    out["scale"]["stable_time"] = _num(s.get("stable_time"), "scale.stable_time", e, 0.5, 30)
    out["scale"]["stable_delta"] = _num(s.get("stable_delta"), "scale.stable_delta", e, 0, 5000)
    out["scale"]["timeout_sec"] = _num(s.get("timeout_sec", 3), "scale.timeout_sec", e, 1, 60)
    out["scale"]["use_indicator_stable"] = bool(s.get("use_indicator_stable"))

    i = f.get("io", {})
    out["io"]["enabled"] = bool(i.get("enabled"))
    out["io"]["host"] = _host(i.get("host"), "io.host", e, required=out["io"]["enabled"] and not out["simulate"]) \
        or out["io"].get("host") or "192.168.1.60"
    out["io"]["port"] = _num(i.get("port"), "io.port", e, 1, 65535, int)
    out["io"]["unit_id"] = _num(i.get("unit_id"), "io.unit_id", e, 0, 255, int)
    out["io"]["coil_base"] = _num(i.get("coil_base", 0), "io.coil_base", e, 0, 65535, int)
    out["io"]["di_base"] = _num(i.get("di_base", 0), "io.di_base", e, 0, 65535, int)
    out["io"]["relays"] = {k: _relay((i.get("relays") or {}).get(k), f"io.relays.{k}", e) for k in RELAYS}
    used = [v for v in out["io"]["relays"].values() if v is not None]
    if len(used) != len(set(used)):
        e["io.relays"] = "один номер реле указан дважды"
    out["io"]["inputs"] = {k: _relay((i.get("inputs") or {}).get(k), f"io.inputs.{k}", e) for k in INPUTS}
    out["io"]["ir_active_level"] = 0 if str(i.get("ir_active_level")) == "0" else 1
    out["io"]["barrier_pulse_sec"] = _num(i.get("barrier_pulse_sec", 1), "io.barrier_pulse_sec", e, 0.2, 10)

    w = f.get("weighing", {})
    out["weighing"]["mode"] = "reverse" if w.get("mode") == "reverse" else "through"
    out["weighing"]["auto_fix"] = bool(w.get("auto_fix"))
    out["weighing"]["clear_time"] = _num(w.get("clear_time"), "weighing.clear_time", e, 0.5, 60)
    out["weighing"]["trip_max_hours"] = _num(w.get("trip_max_hours"), "weighing.trip_max_hours", e, 1, 720, int)
    out["weighing"]["plate_window_sec"] = _num(w.get("plate_window_sec"), "weighing.plate_window_sec", e, 5, 600, int)

    a = f.get("anpr", {})
    out["anpr"]["min_confidence"] = _num(a.get("min_confidence"), "anpr.min_confidence", e, 0.1, 0.99)
    out["anpr"]["interval_ms"] = _num(a.get("interval_ms"), "anpr.interval_ms", e, 50, 5000, int)

    cams, ids = [], set()
    for n, cam in enumerate(f.get("cameras") or []):
        url = str(cam.get("url", "")).strip()
        name = str(cam.get("name", "")).strip() or f"Камера {n + 1}"
        if not url:
            continue
        if not re.match(r"^(rtsp|rtsps|http|https)://", url, re.I):
            e[f"cameras.{n}.url"] = "адрес должен начинаться с rtsp://"
        cid = re.sub(r"[^a-z0-9_]", "", str(cam.get("id") or "").lower()) or f"cam{n + 1}"
        while cid in ids:
            cid += "x"
        ids.add(cid)
        side = cam.get("side") if cam.get("side") in ("a", "b") else None
        cams.append({"id": cid, "name": name, "side": side, "url": url, "anpr": bool(cam.get("anpr"))})
    out["cameras"] = cams

    sv = f.get("server", {})
    out["server"]["port"] = _num(sv.get("port"), "server.port", e, 1, 65535, int)
    tok = str(sv.get("api_token", "")).strip()
    if tok and not re.fullmatch(r"[A-Za-z0-9_\-]{12,128}", tok):
        e["server.api_token"] = "12–128 символов: латиница, цифры, - и _"
    out["server"]["api_token"] = tok or secrets.token_urlsafe(24)
    if sv.get("clear_password"):
        out["server"].pop("settings_password", None)
    elif sv.get("new_password"):
        pw = str(sv["new_password"])
        if len(pw) < 4:
            e["server.new_password"] = "не короче 4 символов"
        else:
            out["server"]["settings_password"] = hash_password(pw)
    if e:
        raise FormError(e)
    return out


# ------------------------------------------------------------------ проверка связи
def test_scale(conn: dict, parser: str, seconds: float = 4.0) -> dict:
    from .scale import parse_cas, parse_generic
    parse = parse_cas if parser != "generic" else parse_generic
    lines: list[str] = []
    t0 = time.time()
    try:
        if conn.get("type") == "serial":
            import serial
            ser = serial.Serial(str(conn["com"]).upper(), baudrate=int(conn.get("baud", 9600)), timeout=0.5)
            read, close = (lambda: ser.read(ser.in_waiting or 1)), ser.close
        else:
            sock = socket.create_connection((str(conn["host"]).strip(), int(conn["port"])), timeout=4)
            sock.settimeout(0.5)

            def read():
                try:
                    return sock.recv(1024)
                except socket.timeout:
                    return b""
            close = sock.close
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "message": f"нет подключения: {ex}"}
    buf = b""
    try:
        while time.time() - t0 < seconds and len(lines) < 8:
            buf += read()
            *ls, buf = re.split(rb"\r\n|\n|\r", buf)
            lines += [ln.decode("latin-1", "replace") for ln in ls if ln.strip()]
    finally:
        close()
    if not lines:
        return {"ok": False, "message": "подключение есть, но индикатор ничего не присылает "
                                        "(включите непрерывную передачу на индикаторе и проверьте скорость порта в NPort)",
                "lines": [], "raw_tail": buf[-80:].decode("latin-1", "replace")}
    parsed = [parse(ln.encode("latin-1")) for ln in lines]
    good = [p for p in parsed if p and (p.weight is not None or p.overload)]
    if not good:
        return {"ok": False, "message": "данные приходят, но формат не распознан — попробуйте формат «другой»",
                "lines": lines[-5:]}
    last = good[-1]
    return {"ok": True, "message": f"вес {last.weight:.0f} кг" + (" (стабилен)" if last.stable else "")
            if last.weight is not None else "перегруз", "lines": lines[-5:]}


def test_io(host: str, port: int, unit: int, coil_base: int = 0, di_base: int = 0) -> dict:
    from .modbus import ModbusTCP
    m = ModbusTCP(str(host).strip(), int(port), int(unit), timeout=3)
    try:
        coils = m.read_coils(int(coil_base), 6)
        try:
            ins = m.read_inputs(int(di_base), 6)
        except Exception:  # noqa: BLE001
            ins = []
        return {"ok": True, "message": "модуль отвечает",
                "relays": ["вкл" if c else "выкл" for c in coils],
                "inputs": ["1" if x else "0" for x in ins]}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "message": f"нет ответа: {ex}"}
    finally:
        m.close()


def pulse_relay(host: str, port: int, unit: int, coil_base: int, relay: int, sec: float = 2.0) -> dict:
    from .modbus import ModbusTCP
    m = ModbusTCP(str(host).strip(), int(port), int(unit), timeout=3)
    try:
        m.write_coil(int(coil_base) + int(relay), True)
        time.sleep(sec)
        m.write_coil(int(coil_base) + int(relay), False)
        return {"ok": True, "message": f"реле {relay} включалось на {sec:.0f} с"}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "message": f"ошибка: {ex}"}
    finally:
        m.close()


def test_camera(url: str) -> dict:
    import cv2
    cap = cv2.VideoCapture(str(url).strip(), cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return {"ok": False, "message": "камера не открылась — проверьте адрес, логин и пароль"}
        frame = None
        for _ in range(15):
            ok, f = cap.read()
            if ok:
                frame = f
                break
        if frame is None:
            return {"ok": False, "message": "поток открыт, но кадров нет"}
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (640, int(h * 640 / w))) if w > 640 else frame
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return {"ok": True, "message": f"есть видео {w}×{h}",
                "image": "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()}
    finally:
        cap.release()
