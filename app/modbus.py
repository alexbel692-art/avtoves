"""Минимальный клиент Modbus TCP (без внешних библиотек) для Moxa ioLogik."""
from __future__ import annotations

import socket
import struct
import threading


class ModbusError(Exception):
    pass


class ModbusTCP:
    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = 2.0):
        self.host, self.port, self.unit, self.timeout = host, port, unit_id, timeout
        self.sock: socket.socket | None = None
        self.tid = 0
        self.lock = threading.Lock()

    def _connect(self):
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(self.timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            part = self.sock.recv(n - len(buf))
            if not part:
                raise ConnectionError("соединение закрыто")
            buf += part
        return buf

    def _request(self, pdu: bytes) -> bytes:
        with self.lock:
            for attempt in (1, 2):
                try:
                    self._connect()
                    self.tid = (self.tid + 1) & 0xFFFF
                    mbap = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit)
                    self.sock.sendall(mbap + pdu)
                    hdr = self._recv(7)
                    tid, _, length, _ = struct.unpack(">HHHB", hdr)
                    resp = self._recv(length - 1)
                    if tid != self.tid:
                        raise ModbusError("несовпадение transaction id")
                    if resp[0] & 0x80:
                        raise ModbusError(f"исключение Modbus, код {resp[1]}")
                    return resp
                except (OSError, ConnectionError):
                    self.close()
                    if attempt == 2:
                        raise

    # FC05
    def write_coil(self, addr: int, value: bool):
        self._request(struct.pack(">BHH", 5, addr, 0xFF00 if value else 0))

    # FC01
    def read_coils(self, addr: int, count: int) -> list[bool]:
        return self._bits(self._request(struct.pack(">BHH", 1, addr, count)), count)

    # FC02
    def read_inputs(self, addr: int, count: int) -> list[bool]:
        return self._bits(self._request(struct.pack(">BHH", 2, addr, count)), count)

    @staticmethod
    def _bits(resp: bytes, count: int) -> list[bool]:
        data = resp[2:2 + resp[1]]
        return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]
