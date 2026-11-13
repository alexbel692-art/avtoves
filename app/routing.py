"""Маршруты по заказам: координаты адресов (Яндекс Геокодер) и порядок объезда
по времени в пути с пробками (Яндекс Матрица расстояний)."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from itertools import combinations

DEFAULTS = {"geocoder_key": "", "router_key": "", "base_address": "", "base_lat": None, "base_lon": None,
            "mode": "truck", "has_trailer": True,
            "geocoder_url": "https://geocode-maps.yandex.ru/v1",
            "router_url": "https://api.routing.yandex.net/v2/distancematrix"}
SECRET = ("geocoder_key", "router_key")
PRECISE = ("exact", "number", "manual")
MATRIX_CELLS = 100          # ограничение Яндекса на один запрос


class RouteError(Exception):
    pass


class Routing:
    def __init__(self, db):
        self.db = db
        with db.lock:
            db.conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.conn.commit()

    def settings(self) -> dict:
        rows = self.db.q("SELECT value FROM kv WHERE key='yandex'")
        out = dict(DEFAULTS)
        if rows:
            out.update(json.loads(rows[0]["value"]))
        return out

    def save(self, new: dict):
        cur = self.settings()
        cur.update({k: v for k, v in new.items() if k in DEFAULTS})
        self.db.x("INSERT INTO kv(key, value) VALUES('yandex', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(cur, ensure_ascii=False),))

    def _get(self, url: str, params: dict, what: str):
        try:
            with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as ex:
            # В адресе запроса есть ключ, поэтому исходная ошибка наружу не выводится
            reason = {401: "ключ API не принят", 403: "ключ API не принят или у него нет доступа к этому сервису",
                      429: "превышен лимит запросов"}.get(ex.code, f"ошибка {ex.code}")
            raise RouteError(f"{what}: {reason}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise RouteError(f"{what}: нет связи с Яндексом") from None

    def geocode(self, address: str) -> dict:
        s = self.settings()
        if not s["geocoder_key"]:
            raise RouteError("Не задан ключ Яндекс Геокодера")
        res = self._get(s["geocoder_url"], {"apikey": s["geocoder_key"], "geocode": address, "lang": "ru_RU",
                                            "format": "json", "results": 1}, "Геокодер")
        try:
            obj = res["response"]["GeoObjectCollection"]["featureMember"][0]["GeoObject"]
            lon, lat = (float(x) for x in obj["Point"]["pos"].split())      # у Яндекса сначала долгота
            meta = obj["metaDataProperty"]["GeocoderMetaData"]
        except (KeyError, IndexError, ValueError, TypeError):
            raise RouteError("Адрес не найден") from None
        return {"lat": lat, "lon": lon, "precision": meta.get("precision") or "other", "text": meta.get("text") or ""}

    def matrix(self, points: list[tuple[float, float]]) -> tuple[list[list[float]], list[list[float]]]:
        """Время (секунды) и расстояние (метры) между всеми точками, с учётом пробок на сейчас."""
        s = self.settings()
        if not s["router_key"]:
            raise RouteError("Не задан ключ Яндекс Матрицы расстояний")
        n = len(points)
        text = [f"{lat:.6f},{lon:.6f}" for lat, lon in points]
        dur = [[0.0] * n for _ in range(n)]
        dist = [[0.0] * n for _ in range(n)]
        step = max(1, MATRIX_CELLS // n)
        if n > MATRIX_CELLS:
            raise RouteError(f"Слишком много точек в маршруте: {n - 1}")
        for start in range(0, n, step):
            params = {"apikey": s["router_key"], "origins": "|".join(text[start:start + step]),
                      "destinations": "|".join(text), "mode": s["mode"]}
            if s["mode"] == "truck" and s["has_trailer"]:
                params["has_trailer"] = "true"
            res = self._get(s["router_url"], params, "Матрица расстояний")
            if res.get("errors"):
                raise RouteError("Матрица расстояний: " + "; ".join(map(str, res["errors"])))
            for i, row in enumerate(res.get("rows") or []):
                for j, cell in enumerate(row.get("elements") or []):
                    if start + i == j:
                        continue
                    if cell.get("status") != "OK":
                        raise RouteError(f"Яндекс не смог построить дорогу между точками {start + i} и {j}: "
                                         "проверьте координаты адресов")
                    dur[start + i][j] = float(cell["duration"]["value"])
                    dist[start + i][j] = float(cell["distance"]["value"])
        return dur, dist


def best_order(cost: list[list[float]]) -> list[int]:
    """Порядок объезда точек 1..n-1 с выездом из точки 0 и возвратом в неё, минимальный по сумме cost.
    До 12 точек — точное решение, больше — ближайший сосед с улучшением перестановками."""
    n = len(cost)
    if n <= 2:
        return list(range(1, n))
    m = n - 1
    if m <= 12:
        best: dict[tuple[int, int], tuple[float, int]] = {}
        for k in range(1, n):
            best[(1 << (k - 1), k)] = (cost[0][k], 0)
        for size in range(2, m + 1):
            for subset in combinations(range(1, n), size):
                bits = sum(1 << (k - 1) for k in subset)
                for k in subset:
                    prev = bits & ~(1 << (k - 1))
                    best[(bits, k)] = min((best[(prev, j)][0] + cost[j][k], j) for j in subset if j != k)
        full = (1 << m) - 1
        _, last = min((best[(full, k)][0] + cost[k][0], k) for k in range(1, n))
        order, bits = [], full
        while last:
            order.append(last)
            bits, last = bits & ~(1 << (last - 1)), best[(bits, last)][1]
        return order[::-1]
    left, order, cur = set(range(1, n)), [], 0
    while left:
        cur = min(left, key=lambda k: cost[cur][k])
        order.append(cur)
        left.remove(cur)

    def total(o):
        path = [0] + o + [0]
        return sum(cost[a][b] for a, b in zip(path, path[1:]))
    improved = True
    while improved:
        improved = False
        for i in range(len(order) - 1):
            for j in range(i + 1, len(order)):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if total(cand) < total(order) - 1e-9:
                    order, improved = cand, True
    return order
