"""Создание config.yaml при установке (вызывается установщиком).

python setup_config.py --template T --out C:\\ProgramData\\AvtoVes\\config.yaml --nport 192.168.1.50 ...
Существующий файл не перезаписывается (кроме --force).

python setup_config.py --fix-port --out config.yaml
  — при обновлении: если порт веб-интерфейса занят другой программой,
    переводит АвтоВес на свободный (начиная с 8780).
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import secrets
import socket
import sys
from pathlib import Path


def _host(v: str, what: str) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    try:
        ipaddress.ip_address(v)
        return v
    except ValueError:
        pass
    if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", v):
        return v
    raise SystemExit(f"Неверный адрес {what}: {v}")


def _port(v: str, what: str) -> int:
    try:
        p = int(str(v).strip())
    except ValueError:
        raise SystemExit(f"Неверный порт {what}: {v}")
    if not 1 <= p <= 65535:
        raise SystemExit(f"Неверный порт {what}: {v}")
    return p


DEFAULT_PORT = 8780


def port_busy(port: int) -> bool:
    """Занят ли порт другой программой (слушает кто-то или нельзя занять)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def free_port(start: int = DEFAULT_PORT) -> int:
    for p in list(range(start, start + 50)) + list(range(DEFAULT_PORT, DEFAULT_PORT + 50)):
        if not port_busy(p):
            return p
    raise SystemExit("Не найден свободный порт для веб-интерфейса")


def _find_server_port(lines: list[str]) -> int | None:
    """Номер строки «  port: N» внутри раздела server: (построчно, без регулярок по всему файлу)."""
    in_server = False
    for i, ln in enumerate(lines):
        if re.match(r"^server:\s*(#.*)?$", ln):
            in_server = True
            continue
        if in_server and re.match(r"^\S", ln):     # начался следующий раздел
            return None
        if in_server and re.match(r"^\s+port:\s*\d+", ln):
            return i
    return None


def _current_port(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    i = _find_server_port(lines)
    return int(re.search(r"\d+", lines[i]).group()) if i is not None else 0


def _report(path: str, port: int):
    if path and port:
        Path(path).write_text(str(port), encoding="ascii")


def fix_port(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    i = _find_server_port(lines)
    if i is None:
        print("порт в настройках не найден — оставляю как есть")
        return 0
    m = re.match(r"^(\s+port:\s*)(\d+)(.*)$", lines[i], re.S)
    port = int(m.group(2))
    if not port_busy(port):
        print(f"порт {port} свободен")
        return port
    new = free_port(DEFAULT_PORT if port != DEFAULT_PORT else DEFAULT_PORT + 1)
    lines[i] = f"{m.group(1)}{new}{m.group(3)}"
    path.write_text("".join(lines), encoding="utf-8")
    print(f"порт {port} занят другой программой — АвтоВес переведён на {new}")
    return new


def _yaml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _web_port(v: str) -> int:
    p = _port(v, "веб-интерфейса")
    if port_busy(p):
        n = free_port(p + 1)
        print(f"порт {p} занят другой программой — использую {n}")
        return n
    return p


def build(args) -> str:
    tpl = Path(args.template).read_text(encoding="utf-8")
    nport = _host(args.nport, "NPort") or "192.168.1.50"
    io_host = _host(args.iologik, "ioLogik")
    cams = []
    for cid, name, side, url in (("cam_a", "Въезд А", "a", args.cam_a), ("cam_b", "Въезд Б", "b", args.cam_b)):
        url = (url or "").strip()
        if not url:
            continue
        if not re.match(r"^(rtsp|rtsps|http|https)://", url, re.I):
            raise SystemExit(f"Адрес камеры должен начинаться с rtsp:// — {url}")
        cams.append(f"  - id: {cid}\n    name: \"{name}\"\n    side: {side}\n"
                    f"    url: {_yaml_str(url)}\n    anpr: true")
    cameras = "cameras:\n" + "\n".join(cams) if cams else "cameras: []"
    repl = {
        "__SIMULATE__": "true" if args.simulate else "false",
        "__WEB_PORT__": str(_web_port(args.web_port)),
        "__TOKEN__": secrets.token_urlsafe(24),
        "__NPORT_HOST__": nport,
        "__NPORT_PORT__": str(_port(args.nport_port, "NPort")),
        "__IO_ENABLED__": "true" if io_host else "false",
        "__IO_HOST__": io_host or "192.168.1.60",
        "__MODE__": "reverse" if args.mode == "reverse" else "through",
        "__CAMERAS__": cameras,
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--nport", default="192.168.1.50")
    ap.add_argument("--nport-port", default="4001")
    ap.add_argument("--iologik", default="")
    ap.add_argument("--cam-a", default="")
    ap.add_argument("--cam-b", default="")
    ap.add_argument("--web-port", default=str(DEFAULT_PORT))
    ap.add_argument("--mode", default="through")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--fix-port", action="store_true")
    ap.add_argument("--check-port", type=int, help="код 1, если порт занят")
    ap.add_argument("--report", default="", help="записать итоговый порт в этот файл")
    args = ap.parse_args(argv)
    if args.check_port:
        return 1 if port_busy(args.check_port) else 0
    out = Path(args.out)
    if args.fix_port:
        if not out.exists():
            return 0
        before = _current_port(out)
        port = fix_port(out)
        _report(args.report, port)
        return 10 if before and port and port != before else 0
    if out.exists() and not args.force:
        print(f"{out} уже есть — оставляю как есть")
        _report(args.report, _current_port(out))
        return 0
    text = build(args)
    import yaml  # проверяем, что получился корректный YAML
    yaml.safe_load(text)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"Создан {out}")
    _report(args.report, _current_port(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
