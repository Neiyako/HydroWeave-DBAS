"""Reusable, auditable proxy diagnostics for ERA5 cloud-water studies."""
from __future__ import annotations

from typing import Any, Mapping
import numpy as np

G = 9.80665


def trapezoid_weights(p_hpa: np.ndarray) -> np.ndarray:
    p = np.asarray(p_hpa, dtype=float)
    if p.ndim != 1 or p.size < 2 or np.any(~np.isfinite(p)) or np.any(np.diff(p) <= 0):
        raise ValueError("pressure levels must be finite and strictly increasing")
    w = np.empty_like(p)
    w[0] = (p[1] - p[0]) / 2
    w[-1] = (p[-1] - p[-2]) / 2
    w[1:-1] = (p[2:] - p[:-2]) / 2
    return w


def precipitable_water(q: np.ndarray, p_hpa: np.ndarray, axis: int = -1) -> np.ndarray:
    """Integrate specific humidity over pressure; returns kg m-2 (numerically mm)."""
    q = np.asarray(q, dtype=float)
    p = np.asarray(p_hpa, dtype=float)
    q = np.moveaxis(q, axis, -1)
    if q.shape[-1] != p.size:
        raise ValueError("pressure axis does not match q")
    w = trapezoid_weights(p)
    return np.nansum(np.where(np.isfinite(q), np.maximum(q, 0), np.nan) * w, axis=-1) * 100 / G


def saturation_vapor_pressure(t_c: np.ndarray, phase: str = "water") -> np.ndarray:
    t = np.asarray(t_c, dtype=float)
    if phase == "water":
        a, b = 17.67, 243.5
    elif phase == "ice":
        a, b = 22.46, 272.62
    else:
        raise ValueError("phase must be 'water' or 'ice'")
    return 611.2 * np.exp(a * t / (t + b))


def relative_humidity(q: np.ndarray, temperature_k: np.ndarray, pressure_hpa: np.ndarray,
                      phase: str = "water", clip: tuple[float, float] = (0, 300)) -> np.ndarray:
    q, temperature_k, pressure_hpa = np.broadcast_arrays(np.asarray(q, float),
        np.asarray(temperature_k, float), np.asarray(pressure_hpa, float))
    e = pressure_hpa * 100 * np.maximum(q, 1e-12) / (0.622 + np.maximum(q, 1e-12))
    es = saturation_vapor_pressure(temperature_k - 273.15, phase)
    out = 100 * e / es
    return np.clip(np.where(np.isfinite(temperature_k), out, np.nan), *clip)


def sub_ground_mask(q: np.ndarray, rtol: float = 1e-3, atol: float = 1e-6):
    """Return usable-level mask and first repeated-fill level along axis 1."""
    a = np.asarray(q, dtype=float)
    if a.ndim != 4:
        raise ValueError("q must have shape (time, level, lat, lon)")
    eq = np.abs(a[:, 1:] - a[:, :-1]) <= atol + rtol * np.abs(a[:, :-1])
    chain = np.logical_and.accumulate(eq[:, ::-1], axis=1)[:, ::-1]
    any_chain = chain.any(axis=1)
    first = np.argmax(chain, axis=1)
    surf = np.where(any_chain, first, a.shape[1] - 1)
    idx = np.arange(a.shape[1])[None, :, None, None]
    return idx <= surf[:, None], surf


def weighted_mean(field: np.ndarray, weights: np.ndarray, mask: np.ndarray | None = None) -> float:
    f, w = np.asarray(field, float), np.asarray(weights, float)
    ok = np.isfinite(f) & np.isfinite(w)
    if mask is not None:
        ok &= np.asarray(mask, bool)
    if not ok.any() or np.nansum(w[ok]) == 0:
        return float("nan")
    return float(np.sum(f[ok] * w[ok]) / np.sum(w[ok]))


def error_metrics(reference: np.ndarray, estimate: np.ndarray) -> dict[str, float]:
    r, e = np.broadcast_arrays(np.asarray(reference, float), np.asarray(estimate, float))
    ok = np.isfinite(r) & np.isfinite(e)
    if not ok.any():
        return {k: float("nan") for k in ("n", "bias", "mae", "rmse", "corr", "relative_mae_pct")}
    d = e[ok] - r[ok]
    corr = float(np.corrcoef(r[ok], e[ok])[0, 1]) if ok.sum() > 1 and np.std(r[ok]) and np.std(e[ok]) else float("nan")
    denom = np.mean(np.abs(r[ok]))
    return {"n": float(ok.sum()), "bias": float(np.mean(d)), "mae": float(np.mean(np.abs(d))),
            "rmse": float(np.sqrt(np.mean(d * d))), "corr": corr,
            "relative_mae_pct": float(100 * np.mean(np.abs(d)) / denom) if denom else float("nan")}


def formula_audit(available: Mapping[str, Any] | Any) -> dict[str, Any]:
    names = set(available if isinstance(available, Mapping) else getattr(available, "variables", {}))
    requirements = {
        "PW": {"required": {"q", "level"}, "formula": "PW = ∫ q dp / g", "unit": "kg m-2 (≈ mm)"},
        "RH500": {"required": {"q", "t", "level"}, "formula": "RH(q,T,p) at 500 hPa", "unit": "%"},
        "CFRAC": {"required": {"q", "t", "level"}, "formula": "fraction of valid 100–500 hPa levels with RH ≥ threshold", "unit": "%"},
        "GCWR": {"required": {"clwc", "ciwc", "precipitation", "evaporation", "boundary_flux"}, "formula": "GQh − GR", "unit": "kg"},
    }
    out = {}
    for key, spec in requirements.items():
        missing = sorted(spec["required"] - names)
        out[key] = {"status": "available" if not missing else "INSUFFICIENT_EVIDENCE",
                    "required": sorted(spec["required"]), "missing": missing,
                    "formula": spec["formula"], "unit": spec["unit"]}
    return out
