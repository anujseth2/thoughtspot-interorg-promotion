"""Deploy the parameterized release/ into a target org (with connection remap).

`--target` is a key in variables/targets.json (org_id + connection name). The same TML
imports into every org; that org's variable values resolve ${...} and the connection is
remapped to the target org's connection. VALIDATE_ONLY runs first and blocks the import
if it fails. Never deletes.

Usage:
  python scripts/deploy.py --target prod [--validate-only]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from services import pipeline

ap = argparse.ArgumentParser()
ap.add_argument("--target", required=True, help="a key in variables/targets.json")
ap.add_argument("--validate-only", action="store_true")
a = ap.parse_args()

r = pipeline.deploy(a.target, validate_only=a.validate_only)
print(f"== deploy release -> target '{r['target']}' (org {r['org']}) ==")
print("validate:")
for v in r["validate"]:
    print(f"   [{v['status']}] {v['type']:18} {v['name']:24} {v.get('error') or ''}")
if r.get("blocked"):
    print("\nBLOCKED — validate failed, nothing imported." if not a.validate_only
          else "\n(validate-only)")
elif r.get("imported"):
    print("import:")
    for v in r["imported"]:
        print(f"   [{v['status']}] {v['type']:18} {v['name']:24} guid={v.get('new_id')}  {v.get('error') or ''}")
