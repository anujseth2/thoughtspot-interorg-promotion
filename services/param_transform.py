"""Parameterize exported TML for cross-org (inter-org) promotion.

ThoughtSpot's "portable TML" rule for moving objects across Orgs needs two things:
  1. obj_id present  -> stable identity across orgs (we keep it; strip guid, which is
     cluster-unique and would pin the object to one org).
  2. per-org-varying data bindings replaced with ${variable} tokens, whose values are
     assigned per org via the Variable API.

This module does (2) for db/schema on Table/View/SQL_View objects (and optionally the
connection's accountName/warehouse/role). Models, Liveboards and Answers carry no db
binding -- they reference tables by obj_id, which resolve per org -- so they are only
guid-stripped.
"""
import json
from typing import List, Set, Tuple

import yaml

import config as C

TYPE_KEYS = ("liveboard", "answer", "model", "worksheet",
             "table", "view", "sql_view", "connection", "pinboard")
TABLE_TYPES = {"table", "view", "sql_view"}

_GUID_KEYS = {"guid", "viz_guid"}


def _strip_guids(node) -> None:
    """Recursively remove cluster-specific guid / viz_guid keys at any depth, keeping
    obj_id (the portable identity). Makes re-import idempotent: ThoughtSpot matches objects
    by obj_id and owns the guids, so a second promotion into an org that already has the
    content updates in place instead of colliding on embedded guids (e.g. liveboard vizzes)."""
    if isinstance(node, dict):
        for key in list(node.keys()):
            if key in _GUID_KEYS:
                node.pop(key, None)
            else:
                _strip_guids(node[key])
    elif isinstance(node, list):
        for item in node:
            _strip_guids(item)


def tml_type(doc: dict):
    for k in TYPE_KEYS:
        if k in doc:
            return k
    return None


def load_tml(edoc: str) -> dict:
    edoc = edoc or ""
    return json.loads(edoc) if edoc.lstrip().startswith("{") else yaml.safe_load(edoc)


def parameterize_doc(doc: dict) -> Tuple[dict, Set[str], List[str]]:
    """Return (parameterized doc, variables-referenced, warnings)."""
    used: Set[str] = set()
    warns: List[str] = []
    typ = tml_type(doc)
    if not typ:
        return doc, used, ["unknown TML type, skipped"]

    _strip_guids(doc)                           # drop cluster-specific guids (incl. embedded
                                                # viz_guid) so re-import is idempotent; obj_id stays
    if not doc.get("obj_id"):
        warns.append("no obj_id -- object is NOT cross-org portable until one is set")

    obj = doc[typ]

    if typ in TABLE_TYPES:
        if obj.get("db"):
            obj["db"] = C.ref(C.DB_VAR); used.add(C.DB_VAR)
        if obj.get("schema"):
            obj["schema"] = C.ref(C.SCHEMA_VAR); used.add(C.SCHEMA_VAR)
        # db_table is usually stable across orgs, so it's left as-is.

    if typ == "connection":
        props = obj.get("properties") if isinstance(obj.get("properties"), dict) else obj
        for field, var in (("accountName", C.ACCOUNT_VAR),
                           ("warehouse", C.WAREHOUSE_VAR), ("role", C.ROLE_VAR)):
            if isinstance(props, dict) and props.get(field):
                props[field] = C.ref(var); used.add(var)

    return doc, used, warns


def retarget_connection(doc: dict, conn_name: str) -> None:
    """Point a table/view connection reference at the target org's connection (by name).

    Used when connections are named per org (e.g. DB Dev -> DB Prod). Sets the name and
    drops the source org's connection obj_id/fqn so it resolves to the target connection.
    Connections that keep a consistent name across orgs don't need this -- use
    CONNECTION_PROPERTY variables for their differing properties instead.
    """
    typ = tml_type(doc)
    if typ in TABLE_TYPES:
        conn = doc[typ].get("connection")
        if isinstance(conn, dict):
            conn["name"] = conn_name
            conn.pop("obj_id", None)
            conn.pop("fqn", None)


def source_bindings(docs: List[dict]) -> List[Tuple[str, str]]:
    """Distinct (db, schema) the table objects are bound to, read BEFORE parameterizing
    replaces them with ${...}. Lets the tool report the real db/schema to the operator
    without any live warehouse introspection (which fails on OAuth/per-user connections)."""
    seen = set()
    for d in docs:
        t = tml_type(d)
        if t in TABLE_TYPES:
            o = d.get(t, {}) or {}
            db, sch = o.get("db", ""), o.get("schema", "")
            if db or sch:
                seen.add((db, sch))
    return sorted(seen)


def parameterize_bundle(docs: List[dict]) -> Tuple[List[dict], Set[str], List[dict]]:
    """Parameterize a bundle. Returns (docs, all-vars-used, warnings)."""
    out, all_used, all_warns = [], set(), []
    for d in docs:
        pd, used, warns = parameterize_doc(d)
        out.append(pd)
        all_used |= used
        name = (pd.get(tml_type(pd) or "", {}) or {}).get("name", "?")
        all_warns += [{"object": name, "issue": w} for w in warns]
    return out, all_used, all_warns
