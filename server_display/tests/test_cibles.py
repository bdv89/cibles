"""Tests for cibles-specific endpoints: search stats, advance-usure, categories,
autocomplete suggestions and reference auto-numbering."""
from __future__ import annotations

from app.utils import reference_prefix, next_reference, emplacement_from_reference


# --- Pure helpers: reference parsing / numbering --------------------------------

def test_reference_prefix_strips_trailing_number():
    assert reference_prefix("Ag-TM2-ABBE-005") == "Ag-TM2-ABBE-"
    assert reference_prefix("AuAg8020-ABBE-001") == "AuAg8020-ABBE-"
    assert reference_prefix("Ag-TM2-ABBE-") == "Ag-TM2-ABBE-"  # no trailing digits
    assert reference_prefix("Maideli Advanced") == "Maideli Advanced"  # supplier name


def test_next_reference_max_plus_one_padded():
    refs = ["Au-TEST-001", "Au-TEST-002", "Ti-TEST-001"]
    assert next_reference(refs, "Au-TEST-") == "Au-TEST-003"
    assert next_reference(refs, "Ti-TEST-") == "Ti-TEST-002"
    assert next_reference(refs, "New-FAM-") == "New-FAM-001"  # new family -> 001
    assert next_reference([], "X-") == "X-001"


def test_emplacement_from_reference():
    # Location = token immediately before the trailing number.
    assert emplacement_from_reference("Ag-TM2-ABBE-005") == "ABBE"
    assert emplacement_from_reference("Au-TM2-SUN-001") == "SUN"
    assert emplacement_from_reference("X-GLAD-004") == "GLAD"
    assert emplacement_from_reference("Ag-P.AGC_G_002") == "AGC"    # AGC family collapses to AGC
    assert emplacement_from_reference("Ag-P.AGC_P_002") == "AGC"    # ...whatever the sub-code (P or G)
    assert emplacement_from_reference("Ag-P.AGC_AGC_001") == "AGC"
    assert emplacement_from_reference("Maideli Advanced") == ""      # no trailing number


def test_search_returns_wear_counts(client_public):
    resp = client_public.get("/api/search")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_all"] == 3
    assert data["count_neuve"] == 2   # Au-TEST-001, Ti-TEST-001
    assert data["count_finie"] == 0
    assert "with_cas" not in data      # chemistry stats removed


def test_search_fts_by_material(client_public):
    resp = client_public.get("/api/search", params={"q": "Au"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert all(row["materiau"] == "Au" for row in data["rows"])


def test_smart_parser_material_and_shape(client_public):
    # Both Au rows are Circulaire -> materiau:Au AND forme:Circulaire -> 2
    r = client_public.get("/api/search", params={"q": "Au Circulaire"})
    assert r.status_code == 200
    assert r.json()["total"] == 2


def test_smart_parser_conjunction_excludes(client_public):
    # No Au is Rectangular -> materiau:Au AND forme:Rectangulaire -> 0
    r = client_public.get("/api/search", params={"q": "Au Rectangulaire"})
    assert r.json()["total"] == 0


def test_smart_parser_shape_only_scoped(client_public):
    # Only the Ti row is Rectangular
    r = client_public.get("/api/search", params={"q": "Rectangulaire"})
    assert r.json()["total"] == 1
    assert r.json()["rows"][0]["materiau"] == "Ti"


def test_search_pagination_offset(client_public):
    # Fixture has 3 rows; page through them 2 at a time.
    page1 = client_public.get("/api/search", params={"limit": 2, "offset": 0}).json()
    assert page1["total"] == 3
    assert page1["returned"] == 2
    ids1 = [r["rowid"] for r in page1["rows"]]

    page2 = client_public.get("/api/search", params={"limit": 2, "offset": 2}).json()
    assert page2["total"] == 3          # total is stable across pages
    assert page2["returned"] == 1
    ids2 = [r["rowid"] for r in page2["rows"]]

    # No overlap between pages -> every row is reachable.
    assert set(ids1).isdisjoint(ids2)
    assert len(set(ids1) | set(ids2)) == 3


def test_smart_parser_shape_abbrev_and_accent(client_public):
    # "rect" abbreviation and accent-insensitivity both resolve to forme:Rectangulaire
    assert client_public.get("/api/search", params={"q": "rect"}).json()["total"] == 1
    assert client_public.get("/api/search", params={"q": "circulaire"}).json()["total"] == 2


def test_advance_usure_cycles_states(client_public):
    # Row 1 starts at Neuve; the cycle loops indefinitely.
    r1 = client_public.post("/api/inventory/1/advance-usure")
    assert r1.status_code == 200
    assert r1.json()["usure"] == "Utilisée"

    r2 = client_public.post("/api/inventory/1/advance-usure")
    assert r2.json()["usure"] == "Finie"

    # Finie loops back to Neuve
    r3 = client_public.post("/api/inventory/1/advance-usure")
    assert r3.json()["usure"] == "Neuve"

    # And keeps cycling
    r4 = client_public.post("/api/inventory/1/advance-usure")
    assert r4.json()["usure"] == "Utilisée"


def test_advance_usure_missing_row(client_public):
    resp = client_public.post("/api/inventory/999/advance-usure")
    assert resp.status_code == 200
    assert resp.json()["success"] is False


def test_categories_grouping(client_public):
    resp = client_public.get("/api/categories")
    assert resp.status_code == 200
    cats = resp.json()["categories"]
    by_key = {c["category"]: c for c in cats}
    # Fixture rows carry no dimension -> empty dimension segment in the 3-part key.
    assert by_key["Au|Circulaire|"]["count"] == 2
    assert by_key["Ti|Rectangulaire|"]["count"] == 1
    assert by_key["Au|Circulaire|"]["dimension"] == ""
    # No thresholds set yet -> nothing below
    assert all(c["below"] is False for c in cats)


def test_categories_exclude_finie_from_stock(client_public):
    # Baseline: both Au circular targets are available (Neuve + Utilisée).
    before = {c["category"]: c for c in client_public.get("/api/categories").json()["categories"]}
    assert before["Au|Circulaire|"]["count"] == 2

    # Mark one Au target as Finie (rowid 1 -> Neuve→Utilisée→Finie).
    for _ in range(2):
        client_public.post("/api/inventory/1/advance-usure")
    row = client_public.get("/api/inventory/1").json()["row"]
    assert row["usure"] == "Finie"

    # The Finie target no longer counts toward available stock.
    after = {c["category"]: c for c in client_public.get("/api/categories").json()["categories"]}
    assert after["Au|Circulaire|"]["count"] == 1


def test_categories_split_by_dimension(client_public):
    # Two Au circular targets with distinct diameters -> two distinct categories.
    client_public.post(
        "/api/inventory",
        json={"materiau": "Al", "reference": "Al-DIM-003", "forme": "Circulaire",
              "diametre": "3", "usure": "Neuve"},
    )
    client_public.post(
        "/api/inventory",
        json={"materiau": "Al", "reference": "Al-DIM-004", "forme": "Circulaire",
              "diametre": "4", "usure": "Neuve"},
    )
    client_public.post(
        "/api/inventory",
        json={"materiau": "Cr", "reference": "Cr-DIM-001", "forme": "Rectangulaire",
              "longueur": "450", "largeur": "150", "usure": "Neuve"},
    )
    by_key = {c["category"]: c for c in client_public.get("/api/categories").json()["categories"]}

    assert by_key["Al|Circulaire|Ø3"]["count"] == 1
    assert by_key["Al|Circulaire|Ø4"]["count"] == 1
    assert by_key["Al|Circulaire|Ø3"]["dimension"] == "Ø3"
    assert by_key["Cr|Rectangulaire|450×150"]["count"] == 1
    assert by_key["Cr|Rectangulaire|450×150"]["dimension"] == "450×150"

    # A threshold on Ø3 must not affect Ø4.
    client_public.put(
        "/api/categories/Al|Circulaire|Ø3", json={"min_count": 5}
    )
    by_key2 = {c["category"]: c for c in client_public.get("/api/categories").json()["categories"]}
    assert by_key2["Al|Circulaire|Ø3"]["below"] is True
    assert by_key2["Al|Circulaire|Ø4"]["below"] is False


def test_suggestions_ordered_by_frequency(client_public):
    data = client_public.get("/api/suggestions").json()
    assert data["materiau"] == ["Au", "Ti"]          # Au(2) before Ti(1)
    assert data["forme"] == ["Circulaire", "Rectangulaire"]
    assert data["fournisseur"] == []                 # empty in fixture
    assert data["purete"] == []


def test_next_reference_endpoint(client_public):
    r = client_public.get("/api/next-reference", params={"prefix": "Au-TEST-"})
    assert r.status_code == 200
    assert r.json()["next"] == "Au-TEST-003"
    # Passing a full reference also works (its trailing number is stripped).
    r2 = client_public.get("/api/next-reference", params={"prefix": "Au-TEST-002"})
    assert r2.json()["next"] == "Au-TEST-003"


def test_create_rejects_duplicate_reference(client_public):
    resp = client_public.post(
        "/api/inventory", json={"materiau": "Au", "reference": "Au-TEST-001"}
    )
    assert resp.json()["success"] is False
    assert "déjà utilisée" in resp.json()["error"]


def test_create_allows_new_reference(client_public):
    resp = client_public.post(
        "/api/inventory", json={"materiau": "Au", "reference": "Au-TEST-999"}
    )
    assert resp.json()["success"] is True


def test_create_auto_derives_emplacement(client_public):
    resp = client_public.post(
        "/api/inventory", json={"materiau": "Ag", "reference": "Ag-TM2-ABBE-050"}
    )
    assert resp.json()["success"] is True
    rowid = resp.json()["rowid"]
    row = client_public.get(f"/api/inventory/{rowid}").json()["row"]
    assert row["emplacement"] == "ABBE"


def test_create_explicit_emplacement_wins(client_public):
    resp = client_public.post(
        "/api/inventory",
        json={"materiau": "Ag", "reference": "Ag-TM2-ABBE-051", "emplacement": "MANUEL"},
    )
    rowid = resp.json()["rowid"]
    row = client_public.get(f"/api/inventory/{rowid}").json()["row"]
    assert row["emplacement"] == "MANUEL"


def test_category_threshold_flags_below(client_public):
    # Set a threshold above the Au|Circulaire| stock (2) -> below
    put = client_public.put(
        "/api/categories/Au|Circulaire|", json={"min_count": 5}
    )
    assert put.status_code == 200
    assert put.json()["success"] is True

    resp = client_public.get("/api/categories")
    by_key = {c["category"]: c for c in resp.json()["categories"]}
    assert by_key["Au|Circulaire|"]["threshold"] == 5
    assert by_key["Au|Circulaire|"]["below"] is True
    assert by_key["Ti|Rectangulaire|"]["below"] is False

    # Clearing the threshold removes the flag
    client_public.put("/api/categories/Au|Circulaire|", json={"min_count": None})
    resp2 = client_public.get("/api/categories")
    by_key2 = {c["category"]: c for c in resp2.json()["categories"]}
    assert by_key2["Au|Circulaire|"]["threshold"] is None
    assert by_key2["Au|Circulaire|"]["below"] is False
