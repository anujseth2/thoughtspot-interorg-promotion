"""Inter-org promotion pipeline (variable-based).

Verbs (one shared engine for the CLI):
  snapshot    export authoring-org objects (or the bundled seed) -> parameterize ->
              commit `release/` (org-agnostic TML) + a variable manifest to Git.
  setup_vars  create the TABLE_MAPPING variables in the Primary org and assign each
              target org its own values (the per-org data binding).
  deploy      read `release/` from Git and import it into a target org. obj_id is the
              cross-org identity; the org's variable values resolve the ${...} tokens.

Variables are managed from the Primary org (TS_ORG_PRIMARY, default 0).
"""
import json
import os
import re
from pathlib import Path

import yaml

import config as C
from services.ts_client import TSClient
from services.param_transform import (load_tml, parameterize_bundle, tml_type,
                                       retarget_connection, source_bindings, TABLE_TYPES)
from services import variables as V
from services.gh_creds import github_repo, github_token
from services.git_repo import AreaGitRepo, LocalRepo

ROOT = Path(__file__).resolve().parent.parent
# Repo subfolder (GIT_BASE_PATH) + the release/manifest paths are resolved at CALL TIME,
# not import time - so setting GIT_BASE_PATH from the Setup tab (which runs after this module
# is imported) actually takes effect. Reading os.environ at import froze it to the root.
def _base() -> str:
    return os.environ.get("GIT_BASE_PATH", "").strip().replace("\\", "/").strip("/")

def _release_area() -> str:
    b = _base()
    return f"{b}/release" if b else "release"          # Git folder for the parameterized TML

def _manifest_path() -> str:
    b = _base()
    return f"{b}/variables/manifest.json" if b else "variables/manifest.json"
_ORDER = {"connection": 0, "table": 1, "view": 1, "sql_view": 1,
          "model": 2, "worksheet": 2, "answer": 3, "liveboard": 4}


def _auth(role: str = "source"):
    """The credential, from the environment. A trusted-auth secret (+ admin user) or an admin
    username/password reaches every org (the client mints a token per org_id at connect time),
    so one credential covers source AND target. A bare bearer token is org-bound, so for the
    bearer case you provide TWO: TS_TOKEN (source) and TS_TOKEN_TARGET (target); role='target'
    swaps in the target token for the deploy side."""
    token = os.environ.get("TS_TOKEN", "")
    if role == "target" and os.environ.get("TS_TOKEN_TARGET", "").strip():
        token = os.environ["TS_TOKEN_TARGET"].strip()      # org-bound target bearer for deploy
    return dict(username=os.environ.get("TS_USER", ""),
                password=os.environ.get("TS_PASSWORD", ""),
                token=token,
                secret_key=os.environ.get("TS_SECRET_KEY", ""))


def _orgs_config() -> dict:
    """Per-org config from variables/orgs.json: {org_id: {name, role, connection, values}}.
    {} if the file is absent. role is a list of source/variables/target. No credentials
    here - the one credential above is used for every org."""
    p = ROOT / "variables" / "orgs.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _verify():
    """TLS verification for every request: a CA bundle path (TS_CA_BUNDLE) if set, else
    False when TS_VERIFY_SSL is off (trusted corporate proxy), else True. Lets the tool work
    behind a TLS-inspection proxy without depending on pip-system-certs / the OS trust store."""
    ca = os.environ.get("TS_CA_BUNDLE", "").strip()
    if ca:
        return ca
    if os.environ.get("TS_VERIFY_SSL", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return True


def _primary_org() -> str:
    """Org that manages the variables: the org tagged role 'variables' in orgs.json,
    else TS_ORG_PRIMARY (default 0)."""
    for oid, rec in _orgs_config().items():
        if "variables" in (rec.get("role") or []):
            return str(oid)
    return os.environ.get("TS_ORG_PRIMARY", "0")


def primary_client() -> TSClient:
    return TSClient(host=os.environ["TS_HOST"], org_id=_primary_org(), verify=_verify(), **_auth())


def org_client(org, role: str = "source") -> TSClient:
    return TSClient(host=os.environ["TS_HOST"], org_id=str(org), verify=_verify(), **_auth(role))


def git():
    """GIT_LOCAL_DIR set -> read/write the release in that local folder (any git clone;
    you push/PR yourself). Otherwise commit to the GitHub repo over the API."""
    local = os.environ.get("GIT_LOCAL_DIR")
    if local:
        return LocalRepo(local)
    base = os.environ.get("GIT_BASE_BRANCH", "main").strip() or "main"
    return AreaGitRepo(github_token(), github_repo(), main_branch=base,
                       verify=_verify(), base_url=os.environ.get("GITHUB_API_URL", "").strip())


def _branch():
    """Release branch for the GitHub backend: snapshot commits here and opens a PR into the
    base branch (GIT_BASE_BRANCH, default main) - works with a protected base (which rejects
    direct pushes). None -> commit straight to the base branch, or local-folder mode
    (GIT_LOCAL_DIR), where branches don't apply."""
    if os.environ.get("GIT_LOCAL_DIR"):
        return None
    return os.environ.get("GIT_BRANCH") or None


def _filename(doc: dict) -> str:
    typ = tml_type(doc) or "object"
    base = doc.get("obj_id") or (doc.get(typ, {}) or {}).get("name", "object")
    # Use the FULL obj_id (the cross-org identity) as the filename base. Do NOT split on "__":
    # that was an intra-org area-tool convention (obj_id = base__area) and it collapses real
    # names that contain "__" (e.g. DIM_BUREAU_ACCOUNTS__PRODUCER/__POLICY/__ACCOUNT all mapped
    # to one file -> tables silently overwritten in the release).
    base = re.sub(r"[^0-9A-Za-z]+", "_", base).strip("_").lower() or "object"
    return f"{base}.{typ}.tml"


def list_source_assets(source_org=None, types=None):
    """[{id, name, obj_id, type}] of objects in the source org, for the snapshot asset
    picker. Pick a top-level object (Liveboard/Answer/Model) and its dependencies come
    along on export, so you don't have to select the underlying tables."""
    ts = org_client(source_org or os.environ.get("TS_ORG_SOURCE", "0"))
    return ts.list_objects(types or ["LIVEBOARD", "ANSWER", "LOGICAL_TABLE"])


def list_source_collections(source_org=None):
    """[{id, name, description}] of collections in the source org, for the snapshot
    picker. Requires the Collections beta (26.4.0.cl+, off by default)."""
    ts = org_client(source_org or os.environ.get("TS_ORG_SOURCE", "0"))
    return ts.list_collections()


def preview_dependencies(object_ids, source_org=None):
    """Resolve what a set of picked assets will ACTUALLY promote — each selected asset + its full
    dependency chain (model, tables) — so the operator sees it before snapshotting. Resolved
    PER selected asset (one export per asset) so the UI can show which dependency belongs to which
    pick; shared tables legitimately appear under each asset that uses them. Same export path the
    snapshot uses. Returns {groups: [{root_id, objects:[{name,type,obj_id}]}], failures:[...]}."""
    ts = org_client(source_org or os.environ.get("TS_ORG_SOURCE", "0"))
    groups, failures = [], []
    for oid in object_ids:
        edocs, fails = ts.export_associated([oid])
        objs = []
        for e in edocs:
            d = load_tml(e)
            t = tml_type(d) or "object"
            objs.append({"name": (d.get(t, {}) or {}).get("name", ""), "type": t, "obj_id": d.get("obj_id", "")})
        groups.append({"root_id": oid, "objects": objs})
        failures += fails
    return {"groups": groups, "failures": failures}


def list_source_tags(source_org=None):
    """All tag names in the source org, so the operator can pick a tag instead of typing blind."""
    ts = org_client(source_org or os.environ.get("TS_ORG_SOURCE", "0"))
    data = ts._post("/api/rest/2.0/metadata/search", {"metadata": [{"type": "TAG"}], "record_size": -1})
    items = data if isinstance(data, list) else data.get("metadata", [])
    return sorted({it.get("metadata_name", "") for it in items if it.get("metadata_name")})


def list_tagged(tag, source_org=None):
    """[{id, name, obj_id, type}] of objects carrying `tag` in the source org (the roots a tag
    snapshot promotes; dependencies resolve on export)."""
    ts = org_client(source_org or os.environ.get("TS_ORG_SOURCE", "0"))
    return ts.search_by_tag(tag, ["LOGICAL_TABLE", "LIVEBOARD", "ANSWER"])


def resolve_collection(collection_id, source_org=None):
    """[{id, name, type}] of a collection's promotable members in the source org,
    recursing into sub-collections. For the snapshot preview."""
    ts = org_client(source_org or os.environ.get("TS_ORG_SOURCE", "0"))
    return ts.resolve_collection(collection_id)


def snapshot(source_org=None, tag=None, from_seed=False, object_ids=None,
             collection=None) -> dict:
    g = git()
    if from_seed:
        docs = [load_tml(p.read_text()) for p in sorted((ROOT / "seed").glob("*.tml"))]
    else:
        ts = org_client(source_org or os.environ.get("TS_ORG_SOURCE", "0"))
        types = ["LOGICAL_TABLE", "LIVEBOARD", "ANSWER"]
        if object_ids:                             # explicit asset selection (deps pulled in)
            ids = list(object_ids)
        elif collection:                           # scope the release to a collection (recursed)
            found = ts.resolve_collection(collection)
            if not found:
                raise RuntimeError(f"collection '{collection}' has no promotable members "
                                   "in the source org (empty, or Collections beta not enabled)")
            ids = [f["id"] for f in found]
        elif tag:                                  # scope the release to a tag
            found = ts.search_by_tag(tag, types)
            if not found:
                raise RuntimeError(f"no objects tagged '{tag}' in the source org")
            ids = [f["id"] for f in found]
        else:                                      # ALL assets in the org
            found = ts.list_objects(types)
            if not found:
                raise RuntimeError("no objects found in the source org")
            ids = [f["id"] for f in found]
        edocs, failures = ts.export_associated(ids)
        if failures:
            # Do NOT silently drop objects TS couldn't export - a missing dependency (e.g. a
            # model/table the connecting user can't access) would otherwise produce a thin
            # release that looks complete. Abort loudly so it's fixed at the source.
            lines = "\n  - ".join(f"{f['type']} '{f['name']}' -> {f['status']}: {f['error']}"
                                  for f in failures)
            raise RuntimeError(
                f"Export incomplete: {len(failures)} object(s) could not be exported and would be "
                f"MISSING from the release. This usually means the connecting user lacks access to a "
                f"dependency (model/table). Grant access in the source org and re-run.\n  - {lines}")
        if not edocs:
            raise RuntimeError("Export returned no objects - nothing to snapshot "
                               "(check the selection and the connecting user's access).")
        docs = [load_tml(e) for e in edocs]

    bindings = source_bindings(docs)               # real db/schema, read before parameterizing
    out, used, warns = parameterize_bundle(docs)
    files = {}
    for d in out:                                  # guard: never let two objects share a filename
        fn = _filename(d)
        if fn in files:
            raise RuntimeError(f"filename collision on '{fn}' - two objects would map to the same "
                               "release file (one silently dropped). Report this obj_id/name.")
        files[fn] = yaml.safe_dump(d, sort_keys=False, width=120)
    # per-object manifest so the UI can show friendly Name/Type + the editable obj_id
    objects = [{"file": _filename(d), "name": (d.get(tml_type(d), {}) or {}).get("name", ""),
                "type": tml_type(d) or "object", "obj_id": d.get("obj_id", "")} for d in out]
    branch = _branch()
    release = _release_area()                       # resolve subfolder at call time
    # On a release branch, reset from main each snapshot for a clean single commit + PR;
    # branch=None commits straight to main (unprotected repos / local mode), as before.
    sha = g.commit_area(release, files, message="snapshot parameterized release",
                        branch=branch, reset_from=(g.main if branch else None))
    g.put_file(_manifest_path(), json.dumps(sorted(used), indent=2),
               "chore: variable manifest", branch=branch)
    # prune stale files left by a previous (different) snapshot, so a release fully replaces
    pruned = [fn for fn in list(g.read_area(release, ref=branch)) if fn.endswith(".tml") and fn not in files
              and g.delete_file(f"{release}/{fn}", "chore: drop stale release file", branch=branch)]
    pr_url = None
    if branch:                                  # open (or reuse) a PR into main for review/merge
        try:
            pr_url = g.open_pr(branch, "ThoughtSpot inter-org release",
                               f"Parameterized `release/` snapshot. Review and merge to record it on `{g.main}`.")
        except Exception as e:
            warns.append(f"committed to '{branch}', but no PR opened: {str(e)[:140]}")
    return {"files": list(files), "objects": objects, "variables": sorted(used), "warnings": warns,
            "sha": sha, "pruned": pruned, "branch": branch, "pr_url": pr_url,
            "source_bindings": [{"db": d, "schema": s} for d, s in bindings]}


def setup_vars(values_by_org: dict) -> dict:
    """values_by_org: {org_identifier: {var_name: value}}. Creates the TABLE_MAPPING
    variables in the Primary org (idempotent) and assigns each org its values."""
    pc = primary_client()
    created = [v for v in C.TABLE_MAPPING_VARS if V.ensure_variable(pc, v, "TABLE_MAPPING")]
    assigned = []
    for org, vals in values_by_org.items():
        for var, val in vals.items():
            V.set_org_value(pc, var, str(org), [val], operation="REPLACE")
            assigned.append({"org": org, "variable": var, "value": val})
    return {"created": created, "assigned": assigned}


def _targets() -> dict:
    """Per-target config: {key: {name, org_id, connection, values}}. Sourced from
    orgs.json (records whose role includes 'target'); falls back to a legacy
    variables/targets.json so older configs keep working."""
    cfg = _orgs_config()
    if cfg:
        out = {}
        for oid, rec in cfg.items():
            if "target" in (rec.get("role") or []):
                key = (rec.get("name") or str(oid)).lower().replace(" ", "_")
                out[key] = {"name": rec.get("name", str(oid)), "org_id": str(oid),
                            "connection": rec.get("connection", ""),
                            "values": rec.get("values", {})}
        if out:
            return out
    p = ROOT / "variables" / "targets.json"
    raw = json.loads(p.read_text()) if p.exists() else {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def realign_release(old_to_new: dict) -> dict:
    """Rewrite obj_ids IN THE RELEASE (not the source cluster) to align with the target org's
    existing objects, so deploy updates-in-place instead of creating duplicates. For each
    old->new, text-replaces the obj_id across ALL release files (the object's own obj_id AND every
    reference to it), recomputes the obj_id-based filenames, re-commits, and prunes the old files.
    obj_ids are unique strings, so a text replace is safe. No source-cluster changes."""
    old_to_new = {o: n for o, n in (old_to_new or {}).items() if o and n and o != n}
    if not old_to_new:
        return {"changed": [], "files": []}
    g = git()
    release = _release_area()
    branch = _branch()
    area = {fn: txt for fn, txt in g.read_area(release, ref=branch).items() if fn.endswith(".tml")}
    new_files = {}
    for txt in area.values():
        for old, new in old_to_new.items():
            txt = txt.replace(old, new)
        new_files[_filename(load_tml(txt))] = txt          # filename follows the (new) obj_id
    sha = g.commit_area(release, new_files, message="align obj_ids to target",
                        branch=branch, reset_from=(g.main if branch else None))
    pruned = [fn for fn in list(g.read_area(release, ref=branch)) if fn.endswith(".tml")
              and fn not in new_files
              and g.delete_file(f"{release}/{fn}", "chore: drop renamed release file", branch=branch)]
    return {"changed": list(old_to_new.items()), "files": sorted(new_files), "sha": sha, "pruned": pruned}


def _resolve_var(val, vals):
    """`${ts_db}` -> the target's value; a literal is returned unchanged."""
    if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
        return vals.get(val[2:-1], val)
    return val


def _connection_id(ts, name):
    """GUID of a connection by name in the given org (connection_columns takes an identifier)."""
    data = ts._post("/api/rest/2.0/metadata/search",
                    {"metadata": [{"type": "CONNECTION"}], "record_size": -1})
    items = data if isinstance(data, list) else data.get("metadata", [])
    for it in items:
        if it.get("metadata_name") == name:
            return it.get("metadata_id")
    return ""


def preflight_connection(target: str) -> dict:
    """PROACTIVELY check the target warehouse for schema parity BEFORE deploy, via the ThoughtSpot
    connection API (no direct warehouse connection needed). For every table in the release, confirm
    its physical columns exist in the target org's connection warehouse. Surfaces absent tables and
    warehouse-missing columns (with ready-to-drop `table::col` tokens) so drift is caught before the
    import fails instead of after. Needs DATAMANAGEMENT on the target connection; a table whose
    warehouse can't be introspected (e.g. OAuth/per-user) is reported as 'unchecked', not a failure.
    """
    cfg = _targets().get(target)
    if not cfg:
        raise RuntimeError(f"target '{target}' not in variables/targets.json")
    ts = org_client(cfg["org_id"], role="target")
    conn_name = cfg.get("connection", "")
    vals = cfg.get("values") or {}
    conn_id = _connection_id(ts, conn_name) or conn_name
    files = {k: v for k, v in git().read_area(_release_area(), ref=_branch()).items() if k.endswith(".tml")}
    if not files:
        raise RuntimeError("release/ is empty in Git — run snapshot first")

    tables = [d for d in (load_tml(v) for v in files.values()) if tml_type(d) in TABLE_TYPES]

    def _schema_tables(db, schema):
        """Lowercased set of physical table names the connection exposes for db.schema, or None
        if introspection errored. Empty set = the connection returns no objects (can't introspect)."""
        try:
            data = ts._post("/api/rest/2.0/connection/search", {
                "connections": [{"identifier": conn_id,
                                 "data_warehouse_objects": [{"database": db, "schema": schema}]}],
                "data_warehouse_object_type": "TABLE", "record_size": -1, "record_offset": 0})
        except Exception:
            return None
        conns = data if isinstance(data, list) else data.get("connections", data.get("connection", []))
        out = set()
        for c in conns:
            for dbo in (c.get("data_warehouse_objects") or {}).get("databases", []) or []:
                for sch in dbo.get("schemas", []) or []:
                    for t in sch.get("tables", []) or []:
                        if t.get("name"):
                            out.add(t["name"].lower())
        return out

    # Probe introspectability first: many connections (key-pair/OAuth Snowflake, etc.) return
    # NOTHING via the connection API. If so, don't cry wolf ("every table missing") — report the
    # pre-check as unavailable and let Validate + the import diagnostics catch drift at import time.
    scopes = {}
    for d in tables:
        o = d[tml_type(d)]
        scopes.setdefault((_resolve_var(o.get("db"), vals), _resolve_var(o.get("schema"), vals)), None)
    for key in list(scopes):
        scopes[key] = _schema_tables(*key)
    if not any(scopes.get(k) for k in scopes):       # no scope returned any object
        return {"target": target, "connection": conn_name, "available": False, "clean": None,
                "findings": [], "drop_tokens": [],
                "reason": (f"The '{conn_name}' connection doesn't expose schema introspection via the "
                           "ThoughtSpot connection API (it returned nothing). This is common for "
                           "key-pair/OAuth Snowflake connections. Warehouse drift will still be caught "
                           "at import time by Validate + the import diagnostics.")}

    findings, drop_tokens = [], []
    for d in tables:
        obj = d[tml_type(d)]
        name = obj.get("name", "?")
        db = _resolve_var(obj.get("db"), vals)
        schema = _resolve_var(obj.get("schema"), vals)
        db_table = obj.get("db_table") or name
        tml_cols = [c.get("db_column_name") for c in (obj.get("columns") or []) if c.get("db_column_name")]
        tset = scopes.get((db, schema))
        if tset is None:                             # this scope couldn't be introspected
            findings.append({"table": name, "db_table": db_table, "checked": False,
                             "missing": [], "note": "scope not introspectable"})
            continue
        if db_table.lower() not in tset:
            findings.append({"table": name, "db_table": db_table, "checked": True, "table_absent": True,
                             "missing": [], "note": f"table not found: {db}.{schema}.{db_table}"})
            continue
        wh = ts.connection_columns(conn_id, db, schema, db_table)
        whl = {c.lower() for c in wh}
        missing = [c for c in tml_cols if c.lower() not in whl]
        drop_tokens += [f"{name}::{c}" for c in missing]
        findings.append({"table": name, "db_table": db_table, "checked": True, "table_absent": False,
                         "missing": missing, "note": "ok" if not missing else f"{len(missing)} missing"})

    clean = all(f.get("checked") and not f.get("table_absent") and not f.get("missing") for f in findings)
    return {"target": target, "connection": conn_name, "available": True, "clean": clean,
            "findings": findings, "drop_tokens": sorted(set(drop_tokens))}


def deploy(target: str, validate_only: bool = False, drop_cols=None) -> dict:
    """Deploy release/ into a target org, remapping the connection to that org's.

    `target` is a key in variables/targets.json ({org_id, connection, ...}). Order:
    tables first; VALIDATE_ONLY runs first and a failed validate BLOCKS the import.
    Never deletes. obj_id alignment across orgs is a one-time setup step (align_obj_id),
    not part of deploy, because a physical-match import keeps the existing obj_id.

    On a failed validate the result carries `findings` = classify_import_errors(...), turning
    raw TS errors into reviewer-actionable causes/fixes. `drop_cols` (['table::col', ...]) lets
    the caller re-deploy with warehouse-missing columns (+ their dependent vizs) removed, for
    when the target warehouse lags the source.
    """
    from services.import_diagnostics import classify_import_errors, drop_columns as _drop_columns
    cfg = _targets().get(target)
    if not cfg:
        raise RuntimeError(f"target '{target}' not in variables/targets.json")
    ts = org_client(cfg["org_id"], role="target")   # uses TS_TOKEN_TARGET when set (bearer case)
    files = {k: v for k, v in git().read_area(_release_area(), ref=_branch()).items() if k.endswith(".tml")}
    if not files:
        raise RuntimeError("release/ is empty in Git — run snapshot first")
    docs = [load_tml(v) for v in files.values()]
    dropped = None
    if drop_cols:                                    # remove warehouse-missing columns + dependents
        docs, dropped = _drop_columns(docs, list(drop_cols))
    for d in docs:                                   # remap connection to the target org's
        if cfg.get("connection"):
            retarget_connection(d, cfg["connection"])
    docs.sort(key=lambda d: _ORDER.get(tml_type(d), 9))   # tables -> models -> liveboards
    strings = [json.dumps(d) for d in docs]
    # TS_RESOLVE_LOCAL: bake the target org's values into the ${var} tokens here, instead of
    # relying on the server-side Variable Store. Use when Variables aren't enabled on the cluster.
    if os.environ.get("TS_RESOLVE_LOCAL"):
        for var, val in (cfg.get("values") or {}).items():
            strings = [s.replace("${" + var + "}", val) for s in strings]
    validate = ts.import_tml(strings, policy="VALIDATE_ONLY")
    errs = [r for r in validate if r["status"] != "OK"]
    if validate_only or errs:                        # gate: never import on a failed validate
        return {"target": target, "org": str(cfg["org_id"]), "validate": validate,
                "findings": classify_import_errors(validate) if errs else [],
                "imported": None, "blocked": bool(errs), "dropped": dropped}
    results = ts.import_tml(strings, policy="ALL_OR_NONE")
    return {"target": target, "org": str(cfg["org_id"]), "validate": validate,
            "findings": [], "imported": results, "blocked": False, "dropped": dropped}


def align_obj_id(org, current_obj_id: str, new_obj_id: str) -> dict:
    """Set an object's obj_id in a given org (update-obj-id). Needed to make obj_ids
    consistent across orgs, since a physical-match import keeps the existing obj_id."""
    return org_client(org).set_obj_id(current_obj_id, new_obj_id)
