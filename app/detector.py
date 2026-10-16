#!/usr/bin/env python3
"""Grimoria v2 — persoonsdetectie-backend voor de camerathread.

Model-instructie (deploy op de Pi):
    * Model : SSD MobileNet v2, quantized TFLite (COCO), input 300x300,
              classe "person" = index 0.
    * Pad   : cfg["motion"]["model"] (default "models/ssd_mobilenet_v2.tflite"),
              relatief t.o.v. de app-map; ~4-7 MB, via deploy meegekopieerd.
    * Runtime: `tflite_runtime` (pip-pakket `tflite-runtime`, wordt bij deploy
              op de Pi geïnstalleerd). Als `tensorflow` aanwezig is wordt diens
              `tensorflow.lite.Interpreter` als alternatief geprobeerd.
    * Fallback: als de runtime of het model niet laadt, valt deze module
              terug op FrameDiffDetector (luma-frame-diff, zelfde drempels als
              de huidige cam_watcher in grimoria_pi.py: diff_thr 25,
              min_frac 1,2 %).

Gebruik in de camerathread (picamera2-frame komt als numpy-array binnen;
deze module doet géén camera-aansturing — dat doet de server-integratie):

    det  = PersonDetector(cfg)                 # kiest backend zelf
    det.detect(frame_np)                       # -> {"person", "conf", "bbox"}
    mem  = PresentMemory(cfg["motion"]["present_memory_secs"])
    present = mem.update(detectie, time.time())   # person_present met geheugen

detect() retourneert bbox als [x, y, w, h] in genormaliseerde coords (0..1)
of None. `backend`-property is "tflite" of "frame_diff".
"""

from __future__ import annotations

import os

__all__ = ["PersonDetector", "FrameDiffDetector", "PresentMemory"]

# numpy is nodig voor de detecties (frames komen als numpy-arrays binnen);
# netjes guarden zodat het importeren van de module zelf nooit crasht.
try:
    import numpy as np
    _NP_OK = True
except ImportError:                                    # pragma: no cover
    np = None
    _NP_OK = False

# tflite_runtime guard; tensorflow.lite als alternatief
try:
    from tflite_runtime.interpreter import Interpreter  # type: ignore
    _TFLITE_SRC = "tflite_runtime"
except ImportError:                                    # pragma: no cover
    try:
        from tensorflow.lite import Interpreter        # type: ignore
        _TFLITE_SRC = "tensorflow"
    except ImportError:
        Interpreter = None
        _TFLITE_SRC = None


def _motion_cfg(cfg: dict) -> dict:
    m = (cfg or {}).get("motion")
    return m if isinstance(m, dict) else {}


class FrameDiffDetector:
    """Luma-frame-diff (fallback), gelijk aan de huidige cam_watcher:
    |luma - vorige luma| > diff_thr telt als bewegingspixel; boven
    min_frac van het aantal pixels = "persoon/beweging gezien".

    conf is hier de fractie gewijzigde pixels (geen echte confidence);
    bbox is de bounding box van de gewijzigde pixels (genormaliseerd).
    """

    def __init__(self, cfg: dict | None = None):
        m = _motion_cfg(cfg)
        self._thr = float(m.get("diff_thr", 25))
        self._min_frac = float(m.get("min_frac", 0.012))
        self._prev: "np.ndarray | None" = None

    @property
    def backend(self) -> str:
        return "frame_diff"

    def detect(self, frame_np) -> dict:
        if not _NP_OK:
            raise RuntimeError("numpy ontbreekt; FrameDiffDetector is "
                               "onbruikbaar")
        # 2D-luma maken van gray (H,W) of kleur (H,W,3)
        arr = np.asarray(frame_np)
        if arr.ndim == 3:
            luma = arr[..., :3].mean(axis=2)
        else:
            luma = arr.astype(np.float32)
        luma = luma.astype(np.float32)
        h, w = luma.shape[:2]

        if self._prev is None or self._prev.shape != luma.shape:
            self._prev = luma
            return {"person": False, "conf": None, "bbox": None}

        mask = np.abs(luma - self._prev) > self._thr
        self._prev = luma
        n = int(mask.sum())
        frac = n / float(mask.size) if mask.size else 0.0
        if frac < self._min_frac:
            return {"person": False, "conf": round(frac, 4), "bbox": None}
        ys, xs = np.nonzero(mask)
        x0, x1 = float(xs.min()) / w, float(xs.max()) / w
        y0, y1 = float(ys.min()) / h, float(ys.max()) / h
        bbox = [round(x0, 4), round(y0, 4),
                round(x1 - x0, 4), round(y1 - y0, 4)]
        return {"person": True, "conf": round(frac, 4), "bbox": bbox}


class _TFLitePersonDetector:
    """Wrapper rond een SSD MobileNet v2 TFLite-interpreter (COCO).

    Output-tensors van het standaard detect-model worden op vorm herkend:
    boxes (..., 4), en twee (1, N)-tensors waarvan de scores-tensor waarden
    in [0, 1] heeft — de indices worden na de eerste inferentie vastgelegd.
    """

    INPUT_SIZE = 300
    PERSON_CLASS = 0

    def __init__(self, model_path: str, person_conf: float):
        if Interpreter is None:
            raise ImportError("tflite_runtime ontbreekt")
        if not os.path.isfile(model_path):
            raise FileNotFoundError("model ontbreekt: %s" % model_path)
        self._person_conf = float(person_conf)
        self._interp = Interpreter(model_path=model_path)
        self._interp.allocate_tensors()
        inp = self._interp.get_input_details()[0]
        self._inp_index = inp["index"]
        self._inp_dtype = inp.get("dtype")
        self._out_details = self._interp.get_output_details()
        self._idx_boxes = self._idx_scores = self._idx_classes = None

    @property
    def backend(self) -> str:
        return "tflite"

    # ------------------------------------------------------------ hulpjes
    @staticmethod
    def _resize(frame, size: int):
        """Nearest-neighbour resize naar (size, size[, 3]) met numpy."""
        h, w = frame.shape[:2]
        ys = (np.arange(size) * (h / float(size))).astype(np.int64)
        xs = (np.arange(size) * (w / float(size))).astype(np.int64)
        return frame[ys][:, xs]

    def _resolve_outputs(self) -> None:
        """Bepaal eenmalig welke output-tensor boxes/scores/classes is."""
        for d in self._out_details:
            shape = d.get("shape") or []
            if len(shape) >= 2 and shape[-1] == 4:
                self._idx_boxes = d["index"]
                break
        rest = [d for d in self._out_details
                if d["index"] not in (self._idx_boxes,)
                and len(d.get("shape") or []) == 2]
        for d in rest:
            out = self._interp.get_tensor(d["index"])
            vals = out.reshape(-1).astype("float32")
            if vals.size and float(vals.max()) <= 1.0:
                self._idx_scores = d["index"]
            else:
                self._idx_classes = d["index"]

    # ------------------------------------------------------------ detect
    def detect(self, frame_np) -> dict:
        if not _NP_OK:
            raise RuntimeError("numpy ontbreekt; TFLite-detector is "
                               "onbruikbaar")
        arr = np.asarray(frame_np)
        if arr.ndim == 2:                       # gray -> 3-kanaals
            arr = np.stack([arr, arr, arr], axis=-1)
        small = self._resize(arr, self.INPUT_SIZE)

        if self._inp_dtype is not None and self._inp_dtype.__name__ != "float32":
            inp = small.astype(self._inp_dtype)
        else:
            inp = (small.astype("float32") / 255.0).reshape(
                1, self.INPUT_SIZE, self.INPUT_SIZE, 3)
        self._interp.set_tensor(self._inp_index,
                                inp.reshape(1, self.INPUT_SIZE,
                                            self.INPUT_SIZE, 3))
        self._interp.invoke()
        if self._idx_scores is None:
            self._resolve_outputs()

        boxes = self._interp.get_tensor(self._idx_boxes).reshape(-1, 4)
        scores = self._interp.get_tensor(self._idx_scores).reshape(-1)
        classes = (self._interp.get_tensor(self._idx_classes).reshape(-1)
                   if self._idx_classes is not None else None)

        best = -1.0
        best_i = -1
        for i, sc in enumerate(scores):
            if float(sc) < self._person_conf:
                continue
            if classes is not None and int(classes[i]) != self.PERSON_CLASS:
                continue
            if float(sc) > best:
                best, best_i = float(sc), i
        if best_i < 0:
            return {"person": False, "conf": None, "bbox": None}
        ymin, xmin, ymax, xmax = (float(v) for v in boxes[best_i])
        # genormaliseerd -> xywh (x, y, w, h), geknipt op het frame
        x, y = max(0.0, xmin), max(0.0, ymin)
        w, h = max(0.0, xmax - xmin), max(0.0, ymax - ymin)
        return {"person": True, "conf": round(best, 4),
                "bbox": [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]}


class PersonDetector:
    """Kiest automatisch de TFLite-backend en valt terug op frame-diff.
    Gebruik `PersonDetector(cfg, backend="frame_diff")` om de fallback af
    te dwingen (bijvoorbeeld voor tests)."""

    def __init__(self, cfg: dict | None = None, backend: str | None = None):
        m = _motion_cfg(cfg)
        self._impl = None
        if backend in (None, "tflite") and m.get("detector", "tflite") != "frame_diff":
            model = m.get("model", "models/ssd_mobilenet_v2.tflite")
            if not os.path.isabs(model):
                # relatief pad: t.o.v. de app-map (waar deze module leeft)
                model = os.path.join(os.path.dirname(
                    os.path.abspath(__file__)), model)
            conf = float(m.get("person_conf", 0.5))
            try:
                self._impl = _TFLitePersonDetector(model, conf)
            except (ImportError, FileNotFoundError, Exception):
                if backend == "tflite":
                    raise               # expliciet gevraagd: niet vallen
                self._impl = None       # impliciet: naar frame-diff
        if self._impl is None:
            self._impl = FrameDiffDetector(cfg)

    @property
    def backend(self) -> str:
        return self._impl.backend

    def detect(self, frame_np) -> dict:
        return self._impl.detect(frame_np)


class PresentMemory:
    """Zet losse detecties om naar person_present met geheugen: als de
    persoon minstens één keer gezien is in de laatste memory_secs seconden,
    is person_present True (dempte detectie-hakken tussen detect_fps-pulsen)."""

    def __init__(self, memory_secs: float = 1.0):
        self._mem = float(memory_secs)
        self._last_seen = None

    def update(self, detection: dict, now: float) -> bool:
        """Verwerk één detectie-resultaat op tijdstip `now`; retourneert
        person_present (gezien binnen het geheugen-venster)."""
        if detection and detection.get("person"):
            self._last_seen = float(now)
        if self._last_seen is None:
            return False
        return (float(now) - self._last_seen) <= self._mem

    @property
    def last_seen(self):
        """Epoch van de laatste detectie (None = nooit gezien)."""
        return self._last_seen
