"""Promote Spotter NL (natural-language) instructions — model-level coaching text that guides
how Spotter interprets queries. This is a SEPARATE artifact from feedback and is NOT carried in
TML; it lives only behind the ai/instructions get/set API (Beta 10.15.0.cl+).

`set` is a FULL REPLACE, so:
  * Merge   = union(source, target)  — add the source's instructions, keep the target's own.
  * Replace = source only            — target ends with exactly the source's instructions.

Scope: only GLOBAL exists today (the API enum is GLOBAL-only; a data-model-user scope is a
documented future extension). We promote GLOBAL only, and — because `set` is a full replace of
the whole model — we read the target's blocks and pass any non-GLOBAL block back unchanged so a
future user-scoped block is never clobbered. We deliberately do NOT promote the source's own
non-GLOBAL blocks.

Ported from the cluster-promo tool and made self-contained for THIS tool's TSClient: the
inter-org client exposes only `_post`/`_get`/`set_obj_id`/`import_tml`, so the ai/instructions
primitives are implemented here rather than on the client (ts_client.py is left untouched).
Both a source_client and a target_client (each one org) are passed in.

Availability guard: if the ai/instructions API is missing on the cluster (needs 10.15.0.cl+),
the calls return 404 / feature-not-enabled; we catch that and return a clear "not available"
result instead of throwing.
"""
from typing import Dict, List

import requests

_NEEDS = "Spotter NL instructions not available on this cluster (needs 10.15.0.cl+)"


class _ApiUnavailable(Exception):
    """The ai/instructions endpoint isn't present/enabled on this cluster."""


def _is_unavailable(exc: requests.HTTPError) -> bool:
    """True if an HTTPError looks like 'endpoint/feature not present' (vs a real per-object error)."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    if resp.status_code == 404:
        return True
    body = (getattr(resp, "text", "") or "").lower()
    return any(s in body for s in
               ("not enabled", "feature is not", "not available", "unknown api", "no handler"))


# ── ai/instructions primitives (self-contained: client only needs _post) ─────────────

def _get_nl_instruction_blocks(client, data_source_identifier: str) -> List[Dict]:
    """Raw NL-instruction blocks [{instructions:[...], scope:...}] for a model.
    Raises _ApiUnavailable if the endpoint is missing; returns [] on any other access error."""
    try:
        d = client._post("/api/rest/2.0/ai/instructions/get",
                         {"data_source_identifier": data_source_identifier})
    except requests.HTTPError as e:
        if _is_unavailable(e):
            raise _ApiUnavailable(_NEEDS)
        return []
    return [b for b in (d.get("nl_instructions_info") or []) if isinstance(b, dict)]


def _get_nl_instructions(client, data_source_identifier: str, scope: str = "GLOBAL") -> List[str]:
    """A model's NL instructions for ONE scope (default GLOBAL) as a flat list of strings.
    Scoped on purpose so a future non-GLOBAL block is never read/promoted as if it were global."""
    out: List[str] = []
    for blk in _get_nl_instruction_blocks(client, data_source_identifier):
        if (blk.get("scope") or "GLOBAL") == scope:
            out.extend(blk.get("instructions") or [])
    return out


def _set_nl_instruction_blocks(client, data_source_identifier: str, blocks: List[Dict]) -> bool:
    """Set (FULL REPLACE of the whole model) NL-instruction blocks verbatim, preserving each
    block's scope. The API rejects an empty nl_instructions_info list (400 'Empty Scope is not
    allowed'); to CLEAR GLOBAL you send one block with an empty instructions array."""
    info = [{"instructions": list(b.get("instructions") or []),
             "scope": b.get("scope") or "GLOBAL"} for b in blocks]
    if not info:
        info = [{"instructions": [], "scope": "GLOBAL"}]     # clear GLOBAL (empty list would 400)
    try:
        d = client._post("/api/rest/2.0/ai/instructions/set",
                         {"data_source_identifier": data_source_identifier,
                          "nl_instructions_info": info})
    except requests.HTTPError as e:
        if _is_unavailable(e):
            raise _ApiUnavailable(_NEEDS)
        return False
    return bool(d.get("success", True)) if isinstance(d, dict) else True


def _find_by_obj_id(client, obj_id: str, obj_type: str = "LOGICAL_TABLE"):
    """guid of the object currently holding this obj_id in the client's org (or None)."""
    data = client._post("/api/rest/2.0/metadata/search",
                       {"metadata": [{"type": obj_type}], "record_size": 5000})
    rows = data if isinstance(data, list) else data.get("metadata", [])
    for o in rows:
        if o.get("metadata_obj_id") == obj_id:
            return o.get("metadata_id")
    return None


# ── public API ───────────────────────────────────────────────────────────────────────

def preview(source_client, target_client, source_model_guid: str, target_obj_id: str,
            source_instructions=None) -> Dict:
    """Diff source vs target NL instructions for one model. If source_instructions is provided
    (operator-edited on the select page), it is used verbatim instead of re-fetching the source.
    On a cluster without ai/instructions, returns {available: False, reason: ...}."""
    try:
        src = list(source_instructions) if source_instructions is not None \
            else _get_nl_instructions(source_client, source_model_guid)
        tgt_guid = _find_by_obj_id(target_client, target_obj_id)
        tgt = _get_nl_instructions(target_client, tgt_guid) if tgt_guid else []
    except _ApiUnavailable as e:
        return {"available": False, "reason": str(e),
                "source": [], "target": [], "target_present": False,
                "add": [], "shared": [], "target_only": []}
    return {
        "available":      True,
        "source":         src,
        "target":         tgt,
        "target_present": bool(tgt_guid),
        "add":            [i for i in src if i not in tgt],       # source-only (added on either mode)
        "shared":         [i for i in src if i in tgt],           # already identical
        "target_only":    [i for i in tgt if i not in src],       # kept on Merge, dropped on Replace
    }


def promote(source_client, target_client, models: List[Dict], mode: str = "merge",
            source_map=None) -> List[Dict]:
    """models: [{name, obj_id, source_guid}]. mode: 'merge' (union) | 'replace' (source only).
    source_map: optional {source_guid: [instructions]} of operator-edited instructions to promote
    instead of the source's current ones. Promotes GLOBAL instructions only; preserves any
    non-GLOBAL target block unchanged. Returns a per-model report. On a cluster without
    ai/instructions, returns one 'not available' row per model rather than throwing."""
    report: List[Dict] = []
    try:
        for m in models:
            if source_map is not None and m["source_guid"] in source_map:
                src = list(source_map[m["source_guid"]])                    # operator-edited
            else:
                src = _get_nl_instructions(source_client, m["source_guid"])  # GLOBAL only
            tgt_guid = _find_by_obj_id(target_client, m["obj_id"])
            if not tgt_guid:
                report.append({"model": m["name"], "status": "target model not found",
                               "added": [], "kept": [], "dropped": [], "count": 0})
                continue
            # Split the target's blocks: GLOBAL is what we edit; anything else is passed back as-is.
            tgt: List[str] = []
            other_blocks: List[Dict] = []
            for b in _get_nl_instruction_blocks(target_client, tgt_guid):
                if (b.get("scope") or "GLOBAL") == "GLOBAL":
                    tgt.extend(b.get("instructions") or [])
                else:
                    other_blocks.append(b)                                  # preserve user-/other-scope
            if not src and mode != "replace":
                # nothing to promote and not replacing -> leave the target untouched
                report.append({"model": m["name"], "status": "no source instructions",
                               "added": [], "kept": tgt, "dropped": [], "count": len(tgt)})
                continue
            if mode == "replace":
                final = list(src)
            else:  # merge: source first, then target-only GLOBAL (dedup exact)
                final = list(src) + [i for i in tgt if i not in src]
            blocks = ([{"instructions": final, "scope": "GLOBAL"}] if final else []) + other_blocks
            ok = _set_nl_instruction_blocks(target_client, tgt_guid, blocks)
            report.append({"model": m["name"], "status": "ok" if ok else "failed",
                           "added": [i for i in src if i not in tgt],
                           "kept":  [i for i in tgt if i not in src] if mode != "replace" else [],
                           "dropped": [i for i in tgt if i not in src] if mode == "replace" else [],
                           "count": len(final)})
    except _ApiUnavailable as e:
        return [{"model": m.get("name", "?"), "status": str(e), "available": False,
                 "added": [], "kept": [], "dropped": [], "count": 0} for m in models]
    return report
