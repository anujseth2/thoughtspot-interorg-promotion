"""Create the TABLE_MAPPING variables in the Primary org and assign each target org its
values, reading variables/targets.json.

Usage:  python scripts/setup_vars.py [variables/targets.json]

Variables are managed from the Primary org (TS_ORG_PRIMARY) by an admin.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from services import pipeline

path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "variables" / "targets.json"
targets = {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")}
values_by_org = {cfg["org_id"]: cfg["values"] for cfg in targets.values() if cfg.get("values")}

r = pipeline.setup_vars(values_by_org)
print("created variables:", r["created"] or "(all already existed)")
for a in r["assigned"]:
    print(f"   org {a['org']}: {a['variable']} = {a['value']}")
print(f"\n{len(r['assigned'])} per-org value(s) assigned across {len(values_by_org)} org(s).")
