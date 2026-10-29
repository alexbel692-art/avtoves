"""Светофоры, шлагбаумы и ИК-датчики через Moxa ioLogik (Modbus TCP)."""
from __future__ import annotations

import logging
import threading
import time

from .modbus import ModbusTCP

log = logging.getLogger("io")

LIGHT_OFF, LIGHT_RED, LIGHT_GREEN = "off", "red", "green"


class IOController:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled"))
        self.relays: dict = cfg.get("relays") or {}
        self.inputs_map: dict = cfg.get("inputs") or {}
        self.client = ModbusTCP(cfg["host"], int(cfg["port"]), int(cfg["unit_id"])) if self.enabled else None
        self.lights = {"a": LIGHT_RED, "b": LIGHT_RED}
        self.inputs: dict[str, bool] = {}
        self.connected = not self.enabled
        self.error = ""
        self.manual = False            # ручной режим: автоматика не трогает светофоры
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._poll, name="io", daemon=True)

    def start(self):
        self._thr.start()
        self.apply()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------
    def set_light(self, side: str, state: str, *, force: bool = False):
        if self.manual and not force:
            return
        with self._lock:
            if self.lights.get(side) == state:
                return
            self.lights[side] = state
        log.info("Светофор %s -> %s", side.upper(), state)
        self.apply()

    def apply(self):
        """Записать состояние светофоров в реле (повторяется при каждом опросе)."""
        if not self.enabled:
            return
        try:
            for side in ("a", "b"):
                st = self.lights[side]
                self._coil(f"{side}_red", st == LIGHT_RED)
                self._coil(f"{side}_green", st == LIGHT_GREEN)
            self.connected, self.error = True, ""
        except Exception as e:  # noqa: BLE001
            self.connected, self.error = False, str(e)

    def pulse_barrier(self, side: str):
        name = f"barrier_{side}"
        if self.relays.get(name) is None or not self.enabled:
            return

        def _do():
            try:
                self._coil(name, True)
                time.sleep(float(self.cfg.get("barrier_pulse_sec", 1.0)))
                self._coil(name, False)
            except Exception as e:  # noqa: BLE001
                log.warning("Шлагбаум %s: %s", side, e)

        threading.Thread(target=_do, daemon=True).start()

    def ir_blocked(self) -> bool:
        """True, если хоть один ИК-барьер перекрыт (машина стоит не полностью)."""
        return any(self.inputs.get(k) for k in ("ir_a", "ir_b") if self.inputs_map.get(k) is not None)

    def _coil(self, name: str, value: bool):
        idx = self.relays.get(name)
        if idx is None:
            return
        self.client.write_coil(int(self.cfg["coil_base"]) + int(idx), value)

    def _poll(self):
        active = int(self.cfg.get("ir_active_level", 1))
        used = {k: v for k, v in self.inputs_map.items() if v is not None}
        while not self._stop.wait(0.5):
            if not self.enabled:
                continue
            try:
                if used:
                    n = max(used.values()) + 1
                    bits = self.client.read_inputs(int(self.cfg["di_base"]), n)
                    self.inputs = {k: bits[v] == bool(active) for k, v in used.items()}
                self.apply()   # переподтверждаем состояние (на случай перезагрузки модуля)
            except Exception as e:  # noqa: BLE001
                self.connected, self.error = False, str(e)

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled, "connected": self.connected, "error": self.error,
            "lights": dict(self.lights), "inputs": dict(self.inputs), "manual": self.manual,
            "barriers": [s for s in ("a", "b") if self.relays.get(f"barrier_{s}") is not None],
        }
