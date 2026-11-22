"""Pure category aggregation — shared by the /categories route and the low-stock alert.

A "category" is Matériau + Forme + Dimension (key "materiau|forme|dimension").
The dimension is derived from the per-item columns (Ø + diamètre for round targets,
"longueur×largeur" for rectangular). Available stock counts only targets that are
NOT worn out: a "Finie" target no longer counts toward the category stock.

This module is deliberately framework-free (no FastAPI / no DB driver) so the
standalone alert script can reuse the exact same logic without pulling the web stack.
"""
from __future__ import annotations

from typing import Mapping, Sequence


def _nz(value: str) -> str:
    """A dimension value, or "" if it is empty / zero (mirrors renderDimensions in app.js)."""
    v = (value or "").strip()
    return v if v and v != "0" else ""


def _dim(diametre: str, longueur: str, largeur: str) -> str:
    """Canonical dimension label: "Ø3" for round targets, "450×150" for rectangular, "" if unknown."""
    d = _nz(diametre)
    if d:
        return "Ø" + d
    parts = [p for p in (_nz(longueur), _nz(largeur)) if p]
    return "×".join(parts)


def _key(materiau: str, forme: str, dim: str) -> str:
    return f"{materiau or ''}|{forme or ''}|{dim or ''}"


def aggregate_categories(
    rows: Sequence[Mapping], thresholds: Mapping[str, int]
) -> list[dict]:
    """Aggregate inventory rows into categories with count, threshold and below flag.

    rows: sequence of mappings exposing materiau, forme, diametre, longueur, largeur, usure
          (sqlite3.Row and dict both work).
    thresholds: category_key -> min_count.
    Returns the same structure as the /categories endpoint, sorted by materiau/forme/dimension.
    Available count excludes "Finie" targets.
    """
    agg: dict[str, dict] = {}
    for row in rows:
        materiau = row["materiau"] or ""
        forme = row["forme"] or ""
        dim = _dim(row["diametre"], row["longueur"], row["largeur"])
        key = _key(materiau, forme, dim)
        entry = agg.setdefault(
            key, {"materiau": materiau, "forme": forme, "dimension": dim, "count": 0}
        )
        if (row["usure"] or "").strip() != "Finie":
            entry["count"] += 1

    ordered = sorted(
        agg.items(),
        key=lambda kv: (
            kv[1]["materiau"].lower(),
            kv[1]["forme"].lower(),
            kv[1]["dimension"].lower(),
        ),
    )

    result = []
    for key, entry in ordered:
        count = entry["count"]
        threshold = thresholds.get(key)
        below = threshold is not None and count < threshold
        result.append({
            "category": key,
            "materiau": entry["materiau"],
            "forme": entry["forme"],
            "dimension": entry["dimension"],
            "count": count,
            "threshold": threshold,
            "below": below,
        })
    return result
