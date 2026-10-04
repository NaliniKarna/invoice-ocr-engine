"""Confidence calibration (isotonic regression via pool-adjacent-violators). Only fitted when data supports it.

The system's `confidence` is a rule-based heuristic (method "heuristic-v2"). A calibrated value means:
"among documents scored c, about c were fully correct on the critical fields" — measured on HELD-OUT labelled data.
Until a calibrator is fitted on real labelled data and approved through the registry, calibrated=false everywhere.
"""
from __future__ import annotations

import bisect
import random
from dataclasses import dataclass
from typing import Sequence

from app.ml.metrics import reliability

MIN_CALIBRATION_SAMPLES = 500


class InsufficientCalibrationData(Exception):
    pass


@dataclass
class IsotonicCalibrator:
    xs: list[float]          # right edges of blocks (sorted)
    ys: list[float]          # calibrated value of each block
    n: int

    def apply(self, c: float) -> float:
        if not self.xs:
            return c
        i = bisect.bisect_left(self.xs, c)
        return self.ys[min(i, len(self.ys) - 1)]

    def to_dict(self) -> dict:
        return {"method": "isotonic", "xs": self.xs, "ys": self.ys, "n": self.n}

    @classmethod
    def from_dict(cls, d: dict) -> "IsotonicCalibrator":
        xs, ys = [float(x) for x in d["xs"]], [float(y) for y in d["ys"]]
        if d.get("method") != "isotonic" or len(xs) != len(ys) or xs != sorted(xs) or ys != sorted(ys):
            raise ValueError("invalid calibrator")
        return cls(xs, ys, int(d.get("n", 0)))


def fit_isotonic(conf: Sequence[float], correct: Sequence[bool]) -> IsotonicCalibrator:
    pts = sorted(zip(conf, (1.0 if c else 0.0 for c in correct)))
    blocks: list[list[float]] = []       # [sum, count, right_edge]
    for x, y in pts:
        blocks.append([y, 1, x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
            s, n, r = blocks.pop()
            blocks[-1][0] += s; blocks[-1][1] += n; blocks[-1][2] = r
    return IsotonicCalibrator([b[2] for b in blocks], [b[0] / b[1] for b in blocks], len(pts))


def fit_and_validate(conf: Sequence[float], correct: Sequence[bool], *, seed: int = 0,
                     min_samples: int = MIN_CALIBRATION_SAMPLES) -> tuple[IsotonicCalibrator, dict]:
    """Fits on a random half, reports ECE/Brier before and after on the other half. Refuses small data."""
    n = len(conf)
    if n < min_samples:
        raise InsufficientCalibrationData(f"{n} labelled documents < {min_samples}")
    idx = list(range(n)); random.Random(seed).shuffle(idx)
    fit_i, hold_i = idx[: n // 2], idx[n // 2:]
    cal = fit_isotonic([conf[i] for i in fit_i], [correct[i] for i in fit_i])
    hc = [conf[i] for i in hold_i]; hy = [correct[i] for i in hold_i]
    before = reliability(hc, hy)
    after = reliability([cal.apply(c) for c in hc], hy)
    final = fit_isotonic(conf, correct)        # shipped calibrator uses all data; the holdout numbers describe the method
    return final, {"n": n, "holdout_n": len(hold_i), "ece_before": before["ece"], "ece_after": after["ece"],
                   "brier_before": before["brier"], "brier_after": after["brier"]}
