"""Export authoring-org objects (or the bundled seed) -> parameterize -> commit release/.

Usage:
  python scripts/snapshot.py --from-seed                 # demo: parameterize the seed
  python scripts/snapshot.py --source-org 0 --tag release  # export tagged objects from an org
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
ap.add_argument("--from-seed", action="store_true")
ap.add_argument("--source-org")
ap.add_argument("--tag", help="filter source objects to this tag; omit to snapshot ALL assets")
a = ap.parse_args()

r = pipeline.snapshot(source_org=a.source_org, tag=a.tag, from_seed=a.from_seed)
print(f"committed {r['sha'][:8]} -> release/ ({len(r['files'])} file(s))")
for f in r["files"]:
    print(f"   release/{f}")
print(f"variables referenced (create these): {r['variables']}")
if r["warnings"]:
    print("warnings:", r["warnings"])
