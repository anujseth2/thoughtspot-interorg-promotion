"""
Post-import reconciliation — VERIFY what the deploy actually did against the live target org,
instead of trusting the import response's own OK / new_id claims.

`import_tml` reports a status and a `new_id` per object, but that is what the API *said*, not what
is really on the target: a physical/obj_id match can return a guid that isn't the live object, an
"OK" can land nothing, and a same-named collision can leave two objects behind. This re-queries the
target after the deploy (metadata/search per type) and derives the TRUTH per object:

    created    — the returned new_id exists on target and the name resolves to a single object
    updated    — the name resolves to a single object, but the returned new_id is NOT on target
                 (the import matched/updated a pre-existing object rather than creating that id)
    duplicate  — the name now resolves to two or more objects (a real duplicate)
    missing    — the name resolves to nothing and the new_id is absent (import said OK, but nothing landed)

Reused by app.py Step 3 (Deploy) after a real (non-validate) import. Adapted to the inter-org
ts_client (no GSK-specific calls).
"""
from typing import Dict, List

from services.ts_client import api_metadata_type

# REST metadata types this tool promotes (import result `type` is already a REST enum, but we also
# accept the friendly TML names via api_metadata_type for robustness).
_REST_TYPES = {"LOGICAL_TABLE", "LIVEBOARD", "ANSWER", "CONNECTION"}


def _rest_type(t: str) -> str:
    """Normalise a promoted object's `type` to a REST metadata_type enum."""
    if not t:
        return "LOGICAL_TABLE"
    if t.upper() in _REST_TYPES:
        return t.upper()
    return api_metadata_type(t.lower())


def _search(client, mtype: str) -> List[Dict]:
    """Every object of one type in the target org, as [{name, id}] (id == guid)."""
    data = client._post("/api/rest/2.0/metadata/search",
                        {"metadata": [{"type": mtype}], "record_size": -1})
    rows = data if isinstance(data, list) else data.get("metadata", [])
    return [{"name": o.get("metadata_name"), "id": o.get("metadata_id")} for o in rows]


def reconcile(target_client, promoted: List[Dict]) -> List[Dict]:
    """
    target_client: a TSClient scoped to the target org (the same client the deploy imported with).
    promoted:      the import-result list, [{name, type, new_id, status}], where `type` is a REST
                   metadata_type enum and `new_id` is the guid the import returned.

    Re-queries the target and returns [{name, type, verdict, detail}] with verdict in
    created / updated / duplicate / missing, derived from what is really on the target now.
    """
    # Re-query only the types we actually promoted.
    types = {_rest_type(p.get("type")) for p in promoted}
    rows: List[Dict] = []
    for t in types:
        rows += _search(target_client, t)

    by_name: Dict[str, List[Dict]] = {}
    by_id: Dict[str, Dict] = {}
    for r in rows:
        if r["name"]:
            by_name.setdefault(r["name"], []).append(r)
        if r["id"]:
            by_id[r["id"]] = r

    out: List[Dict] = []
    for p in promoted:
        name = p.get("name")
        new_id = p.get("new_id")
        same_name = by_name.get(name, [])
        id_present = bool(new_id) and new_id in by_id

        if not same_name and not id_present:
            verdict = "missing"
            detail = "import reported OK but no object with this name or id exists on target"
        elif len(same_name) >= 2:
            verdict = "duplicate"
            detail = f"{len(same_name)} objects named '{name}' now exist on target"
        elif id_present:
            verdict = "created"
            detail = f"new_id {new_id} exists on target as '{name}'"
        else:
            # exactly one object by name, but the returned new_id is not on the target:
            # the import matched/updated a pre-existing object instead of creating that id.
            live = same_name[0]
            detail = (f"resolved to existing object {live['id']} "
                      f"(returned new_id {new_id or '(none)'} not present) — updated in place")
            verdict = "updated"

        out.append({"name": name, "type": p.get("type"), "verdict": verdict, "detail": detail})
    return out
