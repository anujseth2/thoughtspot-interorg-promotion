"""Validate the parameterization transform against bundled seed TML (no live org).

Run:  python tests/test_param.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as C
from services.param_transform import load_tml, parameterize_doc, parameterize_bundle


def main():
    # 1 · Table: db/schema become ${...}; guid stripped; obj_id kept
    tbl = load_tml((ROOT / "seed" / "orders.table.tml").read_text())
    before_db, before_schema = tbl["table"].get("db"), tbl["table"].get("schema")
    pd, used, warns = parameterize_doc(tbl)
    t = pd["table"]
    print("=== table parameterization ===")
    print(f"db:      {before_db!r}  ->  {t.get('db')!r}")
    print(f"schema:  {before_schema!r}  ->  {t.get('schema')!r}")
    print(f"obj_id:  {pd.get('obj_id')!r}   guid: {pd.get('guid', '(removed)')!r}")
    print(f"vars used: {sorted(used)}   warnings: {warns or 'none'}")

    assert t["db"] == C.ref(C.DB_VAR), "db must be parameterized to ${ts_db}"
    assert t["schema"] == C.ref(C.SCHEMA_VAR), "schema must be parameterized to ${ts_schema}"
    assert "guid" not in pd, "guid must be stripped"
    assert pd.get("obj_id"), "obj_id (cross-org identity) must be preserved"
    assert C.DB_VAR in used and C.SCHEMA_VAR in used

    # 2 · Model: no db binding -> only guid stripped, no variables referenced
    mdl = load_tml((ROOT / "seed" / "orders_model.model.tml").read_text())
    pm, um, _ = parameterize_doc(mdl)
    print("\n=== model ===")
    print(f"obj_id:  {pm.get('obj_id')!r}   vars used: {sorted(um) or 'none'}")
    assert "guid" not in pm and pm.get("obj_id"), "model keeps obj_id, drops guid"
    assert not um, "model references no db variables (binds to tables by obj_id)"

    # 3 · Bundle: aggregate variable set
    docs = [load_tml((ROOT / "seed" / f).read_text())
            for f in ("orders.table.tml", "orders_model.model.tml", "orders_overview.liveboard.tml")]
    _, all_used, _ = parameterize_bundle(docs)
    print(f"\nbundle variables to create: {sorted(all_used)}")
    assert all_used == {C.DB_VAR, C.SCHEMA_VAR}

    print("\nALL ASSERTIONS PASSED ✓  (same TML deploys everywhere; per-org values fill the ${...})")


if __name__ == "__main__":
    main()
