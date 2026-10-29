"""Камеры (RTSP) и распознавание номеров.

Каждая камера читается в своём потоке (держим только последний кадр, без задержки буфера).
Отдельный поток раз в interval_ms прогоняет свежие кадры через распознаватель.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import cv2
import numpy as np

from .plates import PlateRead, PlateTracker, normalize

log = logging.getLogger("anpr")

# Для RTSP по TCP (меньше рассыпания картинки)
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


class Camera:
    def __init__(self, cfg: dict, source=None):
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.side = cfg.get("side")
        self.url = cfg.get("url", "")
        self.anpr = bool(cfg.get("anpr", True))
        self.source = source            # для имитатора: функция, возвращающая кадр
        self.frame: np.ndarray | None = None
        self.frame_ts = 0.0
        self.lock = threading.Lock()
        self.connected = False
        self.error = ""
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, name=f"cam-{self.id}", daemon=True)

    def start(self):
        self._thr.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        if self.source:
            while not self._stop.wait(0.1):
                self._set(self.source())
            return
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.connected, self.error = False, "не удалось открыть поток"
                self._stop.wait(5)
                continue
            self.error = ""
            fails = 0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 50:
                        break
                    time.sleep(0.02)
                    continue
                fails = 0
                self._set(frame)
            cap.release()
            self.connected, self.error = False, "поток прервался, переподключение"
            self._stop.wait(2)

    def _set(self, frame):
        if frame is None:
            return
        with self.lock:
            self.frame, self.frame_ts = frame, time.time()
        self.connected = True

    def latest(self, max_age: float = 3.0) -> np.ndarray | None:
        with self.lock:
            if self.frame is None or time.time() - self.frame_ts > max_age:
                return None
            return self.frame

    def jpeg(self, width: int = 960, quality: int = 75) -> bytes | None:
        f = self.latest(10)
        if f is None:
            return None
        h, w = f.shape[:2]
        if w > width:
            f = cv2.resize(f, (width, int(h * width / w)))
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    def snapshot(self) -> dict:
        return {"id": self.id, "name": self.name, "side": self.side, "connected": self.connected,
                "error": self.error, "anpr": self.anpr}


# ---------------------------------------------------------------- распознаватели
class FastALPRRecognizer:
    """Локальное распознавание: детектор YOLOv9 + OCR fast-plate-ocr (ONNX, CPU).
    Модели скачиваются автоматически при первом запуске (нужен интернет один раз)."""

    def __init__(self, cfg: dict):
        from fast_alpr import ALPR
        self.alpr = ALPR(detector_model=cfg["detector_model"], ocr_model=cfg["ocr_model"])

    def __call__(self, frame: np.ndarray) -> list[tuple[str, float, tuple[int, int, int, int]]]:
        out = []
        for r in self.alpr.predict(frame):
            if r.ocr is None or not r.ocr.text:
                continue
            bb = r.detection.bounding_box
            conf = r.ocr.confidence
            if isinstance(conf, (list, tuple)):
                conf = float(np.mean(conf)) if conf else 0.0
            out.append((r.ocr.text, float(conf), (int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2))))
        return out


class SimRecognizer:
    """Для режима имитации: «распознаёт» номер, который задал имитатор."""

    def __init__(self, sim):
        self.sim = sim

    def __call__(self, frame, camera_side=None):
        p = self.sim.visible_plate(camera_side)
        return [(p, 0.93, (100, 300, 400, 380))] if p else []


class ANPRService:
    def __init__(self, cfg: dict, cameras: list[Camera], tracker: PlateTracker, recognizer=None):
        self.cfg = cfg
        self.cameras = cameras
        self.tracker = tracker
        self.recognizer = recognizer
        self.error = ""
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, name="anpr", daemon=True)

    def start(self):
        if self.recognizer is None:
            try:
                self.recognizer = FastALPRRecognizer(self.cfg)
                log.info("Распознавание номеров: fast-alpr загружен")
            except Exception as e:  # noqa: BLE001
                self.error = f"распознавание недоступно: {e}"
                log.error(self.error)
                return
        self._thr.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        interval = self.cfg.get("interval_ms", 300) / 1000
        min_conf = float(self.cfg.get("min_confidence", 0.5))
        last_ts: dict[str, float] = {}
        while not self._stop.is_set():
            t0 = time.time()
            for cam in self.cameras:
                if not cam.anpr:
                    continue
                with cam.lock:
                    frame, fts = cam.frame, cam.frame_ts
                if frame is None or fts == last_ts.get(cam.id) or t0 - fts > 3:
                    continue
                last_ts[cam.id] = fts
                try:
                    if isinstance(self.recognizer, SimRecognizer):
                        results = self.recognizer(frame, cam.side)
                    else:
                        results = self.recognizer(frame)
                except Exception as e:  # noqa: BLE001
                    self.error = str(e)
                    log.exception("Ошибка распознавания")
                    continue
                for text, conf, box in results:
                    if conf < min_conf:
                        continue
                    plate, valid = normalize(text)
                    if len(plate) < 6:
                        continue
                    self.tracker.add(PlateRead(fts, cam.id, cam.side, plate, conf, valid,
                                               _crop(frame, box)))
            self._stop.wait(max(0.01, interval - (time.time() - t0)))


def _crop(frame: np.ndarray, box) -> bytes | None:
    x1, y1, x2, y2 = box
    h, w = frame.shape[:2]
    pad_x, pad_y = int((x2 - x1) * 0.15), int((y2 - y1) * 0.3)
    x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
    x2, y2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
    if x2 <= x1 or y2 <= y1:
        return None
    ok, buf = cv2.imencode(".jpg", frame[y1:y2, x1:x2], [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None
