"""Нормализация российских автономеров и голосование по нескольким кадрам."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass

# Буквы, допустимые в номерах РФ (совпадают по начертанию с латиницей)
LAT = "ABEKMHOPCTYX"
CYR = "АВЕКМНОРСТУХ"
TO_CYR = str.maketrans(LAT, CYR)
TO_LAT = str.maketrans(CYR + "ЁЗ", LAT + "E3")

# Типичные ошибки OCR: цифра вместо буквы и наоборот
DIGIT_AS_LETTER = {"0": "O", "8": "B", "4": "A", "7": "T", "3": "E", "1": "T", "6": "B"}
LETTER_AS_DIGIT = {"O": "0", "D": "0", "Q": "0", "B": "8", "I": "1", "L": "1", "Z": "2", "S": "5",
                   "G": "6", "T": "7", "A": "4", "E": "3"}

# Шаблоны (L — буква, D — цифра)
TEMPLATES = [
    "LDDDLLDD", "LDDDLLDDD",     # легковые/грузовые: А123ВС77, А123ВС777
    "LLDDDDDD", "LLDDDDDDD",     # прицепы: АВ123477, АВ1234777 (и такси АВ123777)
    "LLDDDDD",                   # такси/маршрутные: АВ12377
]
_VALID = [re.compile("^" + t.replace("L", f"[{LAT}]").replace("D", r"\d") + "$") for t in set(TEMPLATES)]


def normalize(text: str) -> tuple[str, bool]:
    """Возвращает (номер кириллицей, соответствует ли формату РФ)."""
    s = re.sub(r"[^0-9A-ZА-ЯЁ]", "", (text or "").upper()).translate(TO_LAT)
    if not s:
        return "", False
    if any(p.match(s) for p in _VALID):
        return s.translate(TO_CYR), True
    for tpl in sorted(set(TEMPLATES), key=len):
        if len(tpl) != len(s):
            continue
        fixed = []
        for ch, t in zip(s, tpl):
            if t == "L" and ch.isdigit():
                ch = DIGIT_AS_LETTER.get(ch, ch)
            elif t == "D" and not ch.isdigit():
                ch = LETTER_AS_DIGIT.get(ch, ch)
            fixed.append(ch)
        cand = "".join(fixed)
        if any(p.match(cand) for p in _VALID):
            return cand.translate(TO_CYR), True
    return s.translate(TO_CYR), False


def pretty(plate: str) -> str:
    """А123ВС77 -> А 123 ВС 77 (для отображения)."""
    m = re.match(r"^(\D)(\d{3})(\D{2})(\d{2,3})$", plate or "")
    return " ".join(m.groups()) if m else (plate or "")


@dataclass
class PlateRead:
    ts: float
    camera: str
    side: str | None
    plate: str
    conf: float
    valid: bool
    crop_jpg: bytes | None = None


class PlateTracker:
    """Хранит последние распознавания со всех камер и выбирает лучший номер."""

    def __init__(self, keep_sec: float = 600):
        self.reads: deque[PlateRead] = deque(maxlen=3000)
        self.keep = keep_sec
        self.lock = threading.Lock()

    def add(self, r: PlateRead):
        with self.lock:
            self.reads.append(r)
            while self.reads and r.ts - self.reads[0].ts > self.keep:
                self.reads.popleft()

    def recent(self, n: int = 15) -> list[PlateRead]:
        with self.lock:
            return list(self.reads)[-n:]

    def best(self, since: float, until: float | None = None, arrive_ts: float | None = None,
             ignore: dict[str, float] | None = None, seen_after: float | None = None) -> dict | None:
        """Лучший номер для машины, заехавшей в arrive_ts.
        ignore     — {номер: время} — прочтения уже взвешенных машин раньше этого
                     времени не учитываются (машина стояла на весах или съезжала);
        seen_after — номер должен быть прочитан хотя бы раз после этого момента
                     (машина подъезжала, когда весы уже освободились)."""
        until = until or time.time()
        with self.lock:
            rs = [r for r in self.reads if since <= r.ts <= until and r.plate
                  and not (ignore and r.plate in ignore and r.ts < ignore[r.plate])]
        if not rs:
            return None
        groups: dict[str, list[PlateRead]] = {}
        for r in rs:
            groups.setdefault(r.plate, []).append(r)
        if seen_after:
            fresh = {p: v for p, v in groups.items() if max(i.ts for i in v) >= seen_after}
            groups = fresh or groups
        ref = arrive_ts or until

        def score(items: list[PlateRead]) -> float:
            # уверенность × формат РФ × свежесть (чем ближе к моменту заезда, тем весомее)
            s = 0.0
            for i in items:
                age = max(0.0, ref - i.ts)
                s += i.conf / (1.0 + age / 10.0)
            return s * (2.0 if items[0].valid else 1.0)

        plate, items = max(groups.items(), key=lambda kv: score(kv[1]))
        # Сторона заезда — камера, которая видела номер последней перед заездом
        lim = (arrive_ts or until) + 3
        before = [i for i in items if i.ts <= lim] or items
        first = max(before, key=lambda i: i.ts)
        best_crop = max(items, key=lambda i: i.conf)
        return {
            "plate": plate,
            "conf": round(max(i.conf for i in items), 3),
            "votes": len(items),
            "valid": items[0].valid,
            "side": first.side,
            "camera": first.camera,
            "crop": best_crop.crop_jpg,
            "alternatives": sorted(((p, len(v)) for p, v in groups.items() if p != plate),
                                   key=lambda x: -x[1])[:3],
        }
