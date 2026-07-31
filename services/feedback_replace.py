"""Spotter feedback: merge-preview + optional Replace.

The platform has NO API to delete/clear feedback entries (metadata/delete rejects FEEDBACK;
there is no ai/feedback endpoint; an empty-array import does not clear). Feedback import only
MERGES (add + replace-by-phrase; target-only entries are kept). So the default is a safe merge,
and this module adds:

  * feedback_preview  — diff source vs target feedback by (type, phrase): add / replace / keep.
  * Replace (opt-in)  — make the target end with ONLY the source's feedback, by REBUILDING the
    model: rename the target model's obj_id to free it, let the normal import create a fresh
    model carrying the aligned obj_id + source feedback (clean), re-point the old model's REAL
    (non-feedback) dependents onto the fresh model, then delete the old model IFF it has no real
    dependents left.

Replace is a heavy, destructive rebuild — the app must gate it behind an explicit acknowledgment.

Ported from the cluster-promo tool and made self-contained for THIS tool's TSClient: the
inter-org client exposes only `_post`/`_get`/`set_obj_id`/`import_tml`/`export_tml`, so the
feedback/dependency/delete primitives are implemented here rather than on the client
(ts_client.py is left untouched). obj_id renaming uses the client's `set_obj_id(cur, new)`.

Availability guard: if the FEEDBACK export endpoint is missing on the cluster (needs Spotter
feedback, 10.15.0.cl+), feedback_preview returns a clear "not available" result instead of
throwing. A per-model 400 (the model simply has no feedback) is treated as empty, not an error.
"""
import json
from typing import Dict, List

import requests
import yaml

_REPLACED_SUFFIX = "__replaced"
_NEEDS = "Spotter feedback not available on this cluster (needs 10.15.0.cl+)"


class _ApiUnavailable(Exception):
    """The FEEDBACK export endpoint isn't present/enabled on this cluster."""


def _is_unavailable(exc: requests.HTTPError) -> bool:
    """True only for 'endpoint/feature not present'. A 400 here means the model has no feedback,
    which is NOT unavailability, so it is deliberately excluded."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    if resp.status_code == 404:
        return True
    body = (getattr(resp, "text", "") or "").lower()
    return any(s in body for s in
               ("not enabled", "feature is not", "not available", "unknown api", "no handler"))


def _key(e: dict):
    return (e.get("type", ""), (e.get("feedback_phrase") or "").strip())


# ── feedback / dependency / delete primitives (self-contained) ────────────────────────

def _find_by_obj_id(client, obj_id: str, obj_type: str = "LOGICAL_TABLE"):
    """guid of the object currently holding this obj_id in the client's org (or None)."""
    data = client._post("/api/rest/2.0/metadata/search",
                       {"metadata": [{"type": obj_type}], "record_size": 5000})
    rows = data if isinstance(data, list) else data.get("metadata", [])
    for o in rows:
        if o.get("metadata_obj_id") == obj_id:
            return o.get("metadata_id")
    return None


def _export_feedback_entries(client, model_guid: str) -> List[Dict]:
    """A model's CURRENT feedback entries (list of dicts). [] if the model has no feedback (400);
    raises _ApiUnavailable if the FEEDBACK export endpoint itself is missing (404)."""
    payload = {"metadata": [{"type": "FEEDBACK", "identifier": model_guid}],
               "export_options": {"include_obj_id": True}}
    try:
        raw = client._post("/api/rest/2.0/metadata/tml/export", payload)
    except requests.HTTPError as e:
        if _is_unavailable(e):
            raise _ApiUnavailable(_NEEDS)
        return []                       # 400 (code 10002) = this model simply has no feedback
    items = raw if isinstance(raw, list) else raw.get("object", [])
    for it in items:
        edoc = it.get("edoc", "") or ""
        if not edoc:
            continue
        doc = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)
        if isinstance(doc, dict) and "nls_feedback" in doc:
            return (doc.get("nls_feedback", {}) or {}).get("feedback", []) or []
    return []


_DEP_TYPE_LABEL = {"PINBOARD_ANSWER_BOOK": "liveboard", "QUESTION_ANSWER_BOOK": "answer",
                   "LOGICAL_TABLE": "model/table"}


def _list_dependents(client, guid: str, record_size: int = 500) -> List[Dict]:
    """Cluster-wide dependents of one object via metadata/search + include_dependent_objects.
    Returns [{type,label,name,id,author}]."""
    data = client._post("/api/rest/2.0/metadata/search", {
        "metadata": [{"type": "LOGICAL_TABLE", "identifier": guid}],
        "include_dependent_objects": True,
        "dependent_objects_record_size": record_size,
        "record_size": 1,
    })
    items = data if isinstance(data, list) else data.get("metadata", [])
    out: List[Dict] = []
    for it in items:
        dep = it.get("dependent_objects") or {}
        if not isinstance(dep, dict):
            continue
        for _src_id, by_type in dep.items():
            if not isinstance(by_type, dict):
                continue
            for typ, objs in by_type.items():
                label = _DEP_TYPE_LABEL.get(typ, typ)
                for o in (objs or []):
                    hdr = o.get("header", {}) or {}
                    out.append({
                        "type":   typ,
                        "label":  label,
                        "name":   o.get("name") or hdr.get("name", ""),
                        "id":     o.get("id") or o.get("metadata_id") or hdr.get("id", ""),
                        "author": hdr.get("authorDisplayName") or hdr.get("authorName", ""),
                    })
    return out


def _real_dependents(client, model_guid: str) -> List[Dict]:
    """Dependents EXCLUDING the model's own feedback (type=FEEDBACK) — feedback dies with the
    model, so it must not block deletion."""
    return [d for d in _list_dependents(client, model_guid) if d.get("type") != "FEEDBACK"]


def _delete_metadata(client, obj_type: str, identifier: str) -> bool:
    """Delete a metadata object. True on success (2xx / 204 No Content), False on HTTP error."""
    try:
        client._post("/api/rest/2.0/metadata/delete",
                    {"metadata": [{"type": obj_type, "identifier": identifier}]})
    except requests.HTTPError:
        return False
    return True


def _name_of(client, guid: str) -> str:
    data = client._post("/api/rest/2.0/metadata/search",
                       {"metadata": [{"type": "LOGICAL_TABLE", "identifier": guid}],
                        "record_size": 5})
    rows = data if isinstance(data, list) else data.get("metadata", [])
    for o in rows:
        if o.get("metadata_id") == guid:
            return o.get("metadata_name")
    return guid


def _repoint_dependent(client, dep_guid: str, old_obj_id: str,
                       new_obj_id: str, new_name: str) -> Dict:
    """Re-bind a dependent (answer/liveboard) from old_obj_id to new_obj_id by re-importing it
    with its model refs rewritten. Returns {name, status, error}."""
    raw = client.export_tml([dep_guid])
    items = raw if isinstance(raw, list) else raw.get("object", [])
    if not items:
        return {"name": dep_guid, "status": "ERROR", "error": "export failed"}
    it = items[0]
    edoc = it.get("edoc", "") or ""
    doc = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)
    name = (it.get("info") or {}).get("name", dep_guid)

    def _fix(tables):
        for t in (tables or []):
            if isinstance(t, dict) and t.get("obj_id") == old_obj_id:
                t["obj_id"] = new_obj_id
                t["name"] = new_name
                t["id"] = new_name
                t.pop("fqn", None)

    if "answer" in doc:
        _fix(doc["answer"].get("tables"))
    if "liveboard" in doc:
        for viz in doc["liveboard"].get("visualizations", []):
            _fix((viz.get("answer", {}) or {}).get("tables"))
    res = client.import_tml([json.dumps(doc)], policy="ALL_OR_NONE")
    row = res[0] if res else {}
    return {"name": name, "status": row.get("status", "ERROR"), "error": row.get("error", "")}


# ── public API ───────────────────────────────────────────────────────────────────────

def feedback_preview(target_client, model_name: str, model_obj_id: str,
                     source_entries: List[dict]) -> Dict:
    """Diff source feedback vs the target model's current feedback, keyed by (type, phrase).
    keep = target-only entries (preserved on Merge, dropped on Replace). On a cluster without
    the FEEDBACK endpoint, returns {available: False, reason: ...}."""
    try:
        guid = _find_by_obj_id(target_client, model_obj_id)
        tgt_entries = _export_feedback_entries(target_client, guid) if guid else []
    except _ApiUnavailable as e:
        return {"available": False, "reason": str(e), "model": model_name,
                "target_present": False, "target_guid": None,
                "source": [], "target": [], "source_grouped": {}, "target_grouped": {},
                "add": [], "replace": [], "keep": []}

    def _tok(e):
        return (e.get("search_tokens") or "").strip()
    src_tok = {_key(e): _tok(e) for e in source_entries}   # (type,phrase) -> columns it maps to
    tgt_tok = {_key(e): _tok(e) for e in tgt_entries}
    src, tgt = set(src_tok), set(tgt_tok)

    def _label(t, p):
        kind = "biz term" if t == "BUSINESS_TERM" else ("ref Q" if t == "REFERENCE_QUESTION" else t.lower())
        return f"{p} ({kind})"

    def _grouped(pairs, tokmap):
        # {label: [{phrase, tokens}]} — tokens drives the "?" tooltip in the picker/preview.
        out = {"Reference questions": [], "Business terms": [], "Other": []}
        for (t, p) in sorted(pairs):
            item = {"phrase": p if t in ("REFERENCE_QUESTION", "BUSINESS_TERM") else f"{p} ({t})",
                    "tokens": tokmap.get((t, p), "")}
            key = ("Reference questions" if t == "REFERENCE_QUESTION"
                   else "Business terms" if t == "BUSINESS_TERM" else "Other")
            out[key].append(item)
        return out

    return {
        "available":      True,
        "model":          model_name,
        "target_present": bool(guid),
        "target_guid":    guid,
        "source":  sorted(_label(t, p) for (t, p) in src),   # everything on the source
        "target":  sorted(_label(t, p) for (t, p) in tgt),   # everything on the target now
        "source_grouped": _grouped(src, src_tok),   # source entries grouped by kind (for the dropdown)
        "target_grouped": _grouped(tgt, tgt_tok),   # target entries grouped by kind
        "add":     sorted(_label(t, p) for (t, p) in src if (t, p) not in tgt),
        "replace": sorted(_label(t, p) for (t, p) in src if (t, p) in tgt),
        "keep":    sorted(_label(t, p) for (t, p) in tgt if (t, p) not in src),
    }


def replace_prep(target_client, models: List[Dict]) -> List[Dict]:
    """BEFORE import: for each promoted model {name, obj_id} that already exists on the target,
    capture its real (non-feedback) dependents and rename its obj_id to free it, so the import
    creates a FRESH model with the aligned obj_id. Returns [{name, obj_id, old_guid, real_deps}].
    Models absent on the target (first promotion) are skipped — a normal create already gives a
    clean feedback set."""
    prepped = []
    for m in models:
        obj_id = m["obj_id"]
        guid = _find_by_obj_id(target_client, obj_id)
        if not guid:
            continue
        real_deps = _real_dependents(target_client, guid)
        # Free the aligned obj_id by renaming the existing model's obj_id (client renames BY obj_id).
        target_client.set_obj_id(obj_id, obj_id + _REPLACED_SUFFIX)
        prepped.append({"name": m["name"], "obj_id": obj_id,
                        "old_guid": guid, "real_deps": real_deps})
    return prepped


def replace_finalize(target_client, prepped: List[Dict]) -> List[Dict]:
    """AFTER import (the fresh model + source feedback now hold the aligned obj_id): re-point the
    old model's real dependents onto the fresh model, then delete the old model IFF no real
    (non-feedback) dependents remain. Returns a per-model report."""
    report = []
    for p in prepped:
        obj_id, old_guid = p["obj_id"], p["old_guid"]
        new_guid = _find_by_obj_id(target_client, obj_id)        # freshly-imported model
        new_name = _name_of(target_client, new_guid) if new_guid else p["name"]
        repointed, failed = [], []
        for d in p["real_deps"]:
            r = _repoint_dependent(target_client, d.get("id"),
                                   obj_id + _REPLACED_SUFFIX, obj_id, new_name)
            (repointed if r.get("status") == "OK" else failed).append(r.get("name") or d.get("name"))
        remaining = _real_dependents(target_client, old_guid)
        deleted = False
        if not remaining:
            deleted = _delete_metadata(target_client, "LOGICAL_TABLE", old_guid)
        report.append({
            "model":             p["name"],
            "repointed":         repointed,
            "failed":            failed,
            "old_model_deleted": deleted,
            "kept_deps":         [x.get("name") for x in remaining],
        })
    return report
