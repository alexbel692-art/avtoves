"""Создание config.yaml при установке (вызывается установщиком).

python setup_config.py --template T --out C:\\ProgramData\\AvtoVes\\config.yaml --nport 192.168.1.50 ...
Существующий файл не перезаписывается (кроме --force).
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import secrets
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


def _yaml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
        "__WEB_PORT__": str(_port(args.web_port, "веб-интерфейса")),
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
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--nport", default="192.168.1.50")
    ap.add_argument("--nport-port", default="4001")
    ap.add_argument("--iologik", default="")
    ap.add_argument("--cam-a", default="")
    ap.add_argument("--cam-b", default="")
    ap.add_argument("--web-port", default="8080")
    ap.add_argument("--mode", default="through")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} уже есть — оставляю как есть")
        return 0
    text = build(args)
    import yaml  # проверяем, что получился корректный YAML
    yaml.safe_load(text)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"Создан {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
