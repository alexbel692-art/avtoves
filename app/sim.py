"""Режим имитации: весовой индикатор CAS (TCP как у NPort), Modbus-модуль ioLogik и камеры.
Позволяет проверить весь цикл без оборудования: POST /api/sim/drive."""
from __future__ import annotations

import random
import socket
import socketserver
import struct
import threading
import time

import cv2
import numpy as np

from .plates import TO_LAT


class SimWorld:
    def __init__(self):
        self.weight = 0.0
        self.target = 0.0
        self.plate_visible: tuple[str, str] | None = None   # (номер, сторона)
        self.coils = [False] * 16
        self.inputs = [False] * 16
        self.busy = False
        self.lock = threading.Lock()

    # ---------- сценарий проезда
    def drive(self, plate: str, weight: float, side: str = "a", stay: float = 2.0):
        if self.busy:
            return False
        self.busy = True
        threading.Thread(target=self._drive, args=(plate, weight, side, stay), daemon=True).start()
        return True

    def _drive(self, plate, weight, side, stay):
        try:
            self.plate_visible = (plate, side)
            time.sleep(3)                                # подъезжает к камере
            self._ramp(weight, 3)                        # заезжает
            self.plate_visible = None
            t0 = time.time()
            # ждём, пока загорится красный, затем зелёный на стороне выезда
            # (реле 1 — A зелёный, 3 — B зелёный)
            while time.time() - t0 < 10 and self._exit_green(side):
                time.sleep(0.2)
            while time.time() - t0 < 60 and not self._exit_green(side):
                time.sleep(0.2)
            time.sleep(stay)
            self._ramp(0, 3)                             # съезжает
        finally:
            self.busy = False

    def _exit_green(self, side):
        return self.coils[3] if side == "a" else self.coils[1]

    def _ramp(self, to: float, sec: float):
        start = self.weight
        steps = int(sec * 10)
        for i in range(1, steps + 1):
            self.target = start + (to - start) * i / steps
            time.sleep(0.1)
        self.target = to

    def current_weight(self) -> tuple[float, bool]:
        prev = self.weight
        noise = random.choice([0, 0, 0, 20, -20]) if self.target > 0 else 0
        self.weight = self.target
        w = round((self.target + noise) / 20) * 20
        return w, abs(self.target - prev) < 1

    def visible_plate(self, side):
        if self.plate_visible and self.plate_visible[1] == side:
            return self.plate_visible[0].translate(TO_LAT)   # OCR отдаёт латиницу
        return None

    # ---------- имитатор камеры
    def camera_source(self, side: str, name: str):
        def src():
            img = np.full((540, 960, 3), (60, 70, 60), np.uint8)
            cv2.putText(img, f"SIM {name}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (200, 200, 200), 2)
            cv2.putText(img, time.strftime("%H:%M:%S"), (780, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (200, 200, 200), 2)
            p = self.visible_plate(side)
            if p:
                cv2.rectangle(img, (100, 300), (520, 380), (255, 255, 255), -1)
                cv2.putText(img, p, (115, 360), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)
            return img
        return src


def cas_frame(weight: float, stable: bool) -> bytes:
    sign = b"-" if weight < 0 else b"+"
    val = f"{abs(weight):.0f}".rjust(7).encode()
    return (b"ST" if stable else b"US") + b",GS," + b"\x01\x40" + sign + val + b" kg\r\n"


def start_scale_server(world: SimWorld, port: int = 14001):
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                w, st = world.current_weight()
                try:
                    self.request.sendall(cas_frame(w, st))
                except OSError:
                    return
                time.sleep(0.1)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_modbus_server(world: SimWorld, port: int = 15020):
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            s: socket.socket = self.request
            while True:
                hdr = s.recv(7)
                if len(hdr) < 7:
                    return
                tid, pid, ln, unit = struct.unpack(">HHHB", hdr)
                pdu = s.recv(ln - 1)
                fc = pdu[0]
                if fc == 5:
                    addr, val = struct.unpack(">HH", pdu[1:5])
                    world.coils[addr] = val == 0xFF00
                    resp = pdu
                elif fc in (1, 2):
                    addr, cnt = struct.unpack(">HH", pdu[1:5])
                    src = world.coils if fc == 1 else world.inputs
                    nbytes = (cnt + 7) // 8
                    data = bytearray(nbytes)
                    for i in range(cnt):
                        if src[addr + i]:
                            data[i // 8] |= 1 << (i % 8)
                    resp = bytes([fc, nbytes]) + bytes(data)
                else:
                    resp = bytes([fc | 0x80, 1])
                s.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
