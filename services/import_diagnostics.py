"""Turn ThoughtSpot TML import / VALIDATE_ONLY failures into reviewer-actionable findings,
and apply a reviewer's "drop these columns" choice to the release set — as pure logic over
the in-memory TML docs (no network).

Ported from the cross-CLUSTER promotion tool, adapted to THIS tool's shapes:
  * import results are `[{name,type,status,error,new_id}]` (TSClient.import_tml), status
    "OK"/"WARNING"/"ERROR" — classify_import_errors reads that list.
  * TML items are already-parsed dicts (param_transform.load_tml output), NOT `{"edoc": <str>}`
    wrappers — so the drop-cascade helpers operate on the dicts directly and return dicts,
    ready to json.dumps back into import_tml.

The import-error shapes we translate were observed live (ps-internal):

  missing warehouse column — a column the TML references is absent from the TARGET warehouse.
                Import HARD-FAILS (error_code 14536):
                  "External column with name: <db.schema.table.col> does not exist in
                   connection <conn>."
                Reviewer choice: add the column to the target warehouse and re-run (default),
                OR drop it from the release (+ the joins/formulas/vizzes that used it).

  type mismatch — a column exists on BOTH sides but the SOURCE-declared type doesn't match the
                TARGET warehouse's physical type. HARD-FAILS at VALIDATE_ONLY:
                  "DataType <T> does not match CDW DataType for column with name
                   <db.schema.table.col> in connection <conn>."

  drop blocked by dependents — a target column the release omits still has dependents on the
                target, so import refuses to delete it, naming the column + its dependents.

  dangling reference — the release references something that no longer resolves: an invalid
                formula id, or a model join whose table/column was dropped. TS reports these
                opaquely; we name the objects/columns involved.

  table not found — the referenced table/data source doesn't exist on the target.
"""

import copy
import re

from services.param_transform import load_tml, tml_type

# ── raw-message patterns (the exact strings TS returns) ──────────────────────────────
_MISSING_WH = re.compile(
    r"External column with name:\s*(\S+?)\s+does not exist in connection\s+(.+?)\.", re.I)
_TYPE_MISMATCH = re.compile(
    r"DataType\s+(\S+)\s+does not match CDW DataType for column with name\s+(\S+?)\s+in connection\s+(.+?)\.",
    re.I)
_DEP_HEADER = re.compile(r"Deleted columns have dependents", re.I)
_TABLE_NOT_FOUND = re.compile(
    r"No data source found|No matches found for table|Table (?:with name\s+)?\S+\s+(?:does not exist|not found)",
    re.I)
_JOIN_UNRESOLVED = re.compile(r"Error while translating .*? join|No matches found for table", re.I)
_INVALID_FORMULA = re.compile(r"invalid formula IDs", re.I)
_VIZ_ERR = re.compile(r"Visualization\s*<b>\s*(.*?)\s*</b>\s*has following errors", re.I | re.S)
_FORMULA = re.compile(r"Formula:\s*([^,<]+)", re.I)
_BOLD = re.compile(r"<b>(.*?)</b>", re.I | re.S)
_BRACKET_REF = re.compile(r"\[([^\]]+)\]")


def _clean(msg: str) -> str:
    """Strip ThoughtSpot's HTML flecks: <br/> -> newline, <b>..</b> -> **..**."""
    s = str(msg or "")
    for br in ("<br/>", "<br />", "<br>"):
        s = s.replace(br, "\n")
    return s.replace("<b>", "**").replace("</b>", "**").strip()


# Plain-language translations for the generic (non-column) error shapes TS returns verbatim.
# Each entry: (compiled pattern, lambda match -> (cause, fix)). First match wins.
_ERROR_RULES = [
    (re.compile(r"free trial has ended|warehouses? (?:have|has) been suspended|CONNECTION_CREATION_ERROR", re.I),
     lambda m: ("The target warehouse can't be reached — it looks paused or suspended "
                "(e.g. a Snowflake trial that ended, or a stopped Databricks warehouse).",
                "Resume/resize the warehouse in the data platform, then re-run. This is a warehouse "
                "state problem, not a TML problem.")),
    (re.compile(r"Data source metadata could not be found", re.I),
     lambda m: ("ThoughtSpot couldn't read the connection's metadata.",
                "Usually the warehouse is asleep/suspended or the connection lost its credential — "
                "wake the warehouse or re-test the connection, then re-run.")),
    (re.compile(r"10086|not authorized|permission|privilege|access denied", re.I),
     lambda m: ("Permission problem talking to the connection.",
                "The deploy account needs access to the target connection (shared at edit/MODIFY) "
                "and DATAMANAGEMENT — grant it, then re-run.")),
    (re.compile(r"Existing guid.*will be used", re.I),
     lambda m: ("This object already exists on the target and was updated in place (not an error).",
                "No action needed — this is the normal obj_id update path.")),
    (re.compile(r"timed out|timeout|504|gateway", re.I),
     lambda m: ("The request to the warehouse timed out.",
                "A cold warehouse can exceed the gateway limit — warm it (run a quick query) and "
                "re-run; if it persists it's the connection's column-introspection latency.")),
    (re.compile(r"10054|connection (?:reset|aborted)|forcibly closed|Max retries", re.I),
     lambda m: ("The connection to the target was reset before the request finished.",
                "Usually a slow server-side warehouse validation dropped by a gateway/proxy. Warm "
                "the warehouse (run a quick query) and try again.")),
]


def friendly_error(msg: str):
    """Translate a raw TS error string into (cause, fix, raw_clean) — a short plain-English cause
    and a suggested fix. cause/fix are None when no rule matches, in which case the caller just
    shows the cleaned raw text."""
    raw = _clean(msg)
    for pat, fn in _ERROR_RULES:
        m = pat.search(raw)
        if m:
            cause, fix = fn(m)
            return cause, fix, raw
    return None, None, raw


def classify_import_errors(results):
    """results: the list from TSClient.import_tml — [{name, type, status, error, new_id}] (works
    on a VALIDATE_ONLY result set too). Returns reviewer-actionable findings, each a dict:

      missing_warehouse_column   -> object, column, column_fqn, connection, drop:['table::col'],
                                    error_code=14536, message, fix
      type_mismatch              -> object, column, column_fqn, source_type, connection, message, fix
      drop_blocked_by_dependents -> object, column?, columns[], dependents[], message, fix
      dangling_reference         -> object, formulas[]/tables[], message, fix, error
      table_not_found            -> object, message, fix, error
      viz_error                  -> object, vizzes[], formulas[], message, fix, error
      other                      -> object, message, fix, error

    `drop` on a missing_warehouse_column is the table-qualified token (`table::col`) to hand
    straight to column_drop_cascade / drop_columns — so the reviewer's "drop it" is one call.
    """
    findings = []
    for r in results:
        if (r.get("status") or "").upper() == "OK":
            continue
        msg = r.get("error") or ""
        cause, fix, _raw = friendly_error(msg)      # generic cause/fix, refined per-kind below
        matched = False

        # 14536 — column referenced by the release but absent from the target WAREHOUSE. The
        # validate header name is often "unknown"; the error FQN (db.schema.db_table.col) names
        # the real table, so scope the drop to <table>::<col> (a bare "col" could hit other tables).
        for col_fqn, conn in _MISSING_WH.findall(msg):
            matched = True
            parts = [x for x in col_fqn.split(".") if x]
            table = parts[-2] if len(parts) >= 2 else (r.get("name") or "")
            col = parts[-1] if parts else col_fqn
            findings.append({
                "kind": "missing_warehouse_column", "error_code": 14536,
                "object": table, "column": col, "column_fqn": col_fqn,
                "connection": conn.strip(),
                "drop": [f"{table}::{col}"] if table else [col],
                "message": (f"Column '{col}' on table '{table}' isn't in the target warehouse."),
                "fix": ("Add the column to the target warehouse and re-run, or drop it from the "
                        "release (this removes the joins/formulas/vizzes that use it too)."),
            })

        # type mismatch — column on both sides, source-declared type != target CDW type.
        for src_type, col_fqn, conn in _TYPE_MISMATCH.findall(msg):
            matched = True
            parts = [x for x in col_fqn.split(".") if x]
            table = parts[-2] if len(parts) >= 2 else (r.get("name") or "")
            col = parts[-1] if parts else col_fqn
            findings.append({
                "kind": "type_mismatch", "object": table, "column": col, "column_fqn": col_fqn,
                "source_type": src_type.strip(), "connection": conn.strip(),
                "message": (f"Column '{col}' is typed {src_type.strip()} in the release but the "
                            f"target warehouse has it as a different type."),
                "fix": ("Retype the column to the target's type (column + dependents survive), align "
                        "the target warehouse, or drop it + its dependents."),
            })

        # drop blocked by dependents — target column the release omits still has dependents there.
        if _DEP_HEADER.search(msg):
            matched = True
            made = False
            # Format A: "…- <b>TABLE</b>: Deleted columns have dependents." — names only the table.
            for tbl in re.findall(r"<b>([^<]+)</b>\s*:\s*Deleted columns have dependents", msg):
                if tbl.strip():
                    findings.append({
                        "kind": "drop_blocked_by_dependents", "object": tbl.strip(),
                        "columns": [], "dependents": [],
                        "message": (f"Table '{tbl.strip()}' has columns the release drops that still "
                                    f"have dependents on the target."),
                        "fix": ("Carry the column(s) through (don't drop them), or remove the "
                                "dependent objects on the target first."),
                    })
                    made = True
            # Format B: "Deleted columns have dependents.<br/>- <b>COLUMN</b><ul><li>DEP</li>…</ul>".
            for m in re.finditer(
                    r"Deleted columns have dependents\.\s*<br/?>\s*-\s*<b>([^<]+)</b>(.*?)</ul>",
                    msg, re.S):
                col = m.group(1).strip()
                deps = [d.strip() for d in re.findall(r"<li>([^<]+)</li>", m.group(2)) if d.strip()]
                if col:
                    findings.append({
                        "kind": "drop_blocked_by_dependents", "object": r.get("name"),
                        "column": col, "columns": [col], "dependents": deps,
                        "message": (f"Column '{col}' is dropped by the release but is still used by: "
                                    f"{', '.join(deps) or '(target objects)'}."),
                        "fix": ("Carry the column through, or remove those dependents on the target "
                                "first."),
                    })
                    made = True
            if not made:
                findings.append({
                    "kind": "drop_blocked_by_dependents", "object": r.get("name"),
                    "columns": [], "dependents": [],
                    "message": "A dropped column still has dependents on the target.",
                    "fix": "Carry the column through, or remove its dependents on the target first.",
                })

        # viz error — a liveboard/answer visualization fails to load (e.g. a formula won't compile).
        viz_ids = _VIZ_ERR.findall(msg)
        if viz_ids:
            matched = True
            findings.append({
                "kind": "viz_error", "object": r.get("name"),
                "vizzes": [v.strip() for v in viz_ids],
                "formulas": [f.strip() for f in _FORMULA.findall(msg)],
                "message": (f"{len(viz_ids)} visualization(s) on '{r.get('name')}' failed to load."),
                "fix": ("Fix or drop the offending formula/column, or drop the failing viz so the "
                        "rest of the liveboard imports."),
                "error": msg.strip(),
            })

        # dangling reference (a) invalid formula ids — `formula_<name>` columns that no longer
        # resolve. Dropping those columns (by formula name) resolves it.
        if _INVALID_FORMULA.search(msg):
            matched = True
            fnames = [b.strip() for b in _BOLD.findall(msg)
                      if b.strip() and not b.strip().endswith(":")]
            findings.append({
                "kind": "dangling_reference", "object": r.get("name"), "ref_type": "formula",
                "formulas": fnames,
                "message": (f"Formula reference(s) no longer resolve: {', '.join(fnames) or '(unnamed)'}."),
                "fix": "Drop the affected formula column(s) — dropping cascades their dependents.",
                "error": msg.strip(),
            })

        # dangling reference (b) join can't resolve because a table/column it references was
        # dropped or is unavailable — name the table(s) whose join broke.
        if not matched and _JOIN_UNRESOLVED.search(msg):
            matched = True
            tbls = sorted({t.strip() for t in re.findall(
                r"translating\s+<b>[^<]*</b>\s+join of\s+<b>([^<]+)</b>", msg, re.I) if t.strip()})
            findings.append({
                "kind": "dangling_reference", "object": r.get("name"), "ref_type": "join",
                "tables": tbls,
                "message": (f"A model join can't resolve"
                            + (f" ({', '.join(tbls)})" if tbls else "")
                            + " — a table/column it references is missing."),
                "fix": ("Restore the missing table/column in the release or warehouse, or drop the "
                        "broken table so the model stops referencing it."),
                "error": msg.strip(),
            })

        # table not found — the referenced table / data source doesn't exist on the target.
        if not matched and _TABLE_NOT_FOUND.search(msg):
            matched = True
            findings.append({
                "kind": "table_not_found", "object": r.get("name"),
                "message": (f"A table/data source referenced by '{r.get('name')}' isn't on the target."),
                "fix": ("Promote the missing table first (deploy imports tables before models), or "
                        "check the connection remap points at the right target connection."),
                "error": msg.strip(),
            })

        # anything else — keep the cleaned raw text plus any generic cause/fix friendly_error found.
        if not matched:
            findings.append({
                "kind": "other", "object": r.get("name"),
                "message": cause or _clean(msg) or "Import failed.",
                "fix": fix or "Review the raw error and the object's TML.",
                "error": msg.strip(),
            })
    return findings


# ── column-drop cascade: PURE logic over parsed TML item dicts ────────────────────────
# `items` are already-parsed TML dicts (param_transform.load_tml output). We tolerate a raw
# edoc string too, so the same helpers work on freshly exported TML.

def _doc(item):
    """The TML doc for an item. Items in this tool ARE dicts; a str is parsed as edoc."""
    return load_tml(item) if isinstance(item, str) else item


def _col_name(model_col):
    """A model column's physical name — the tail of `table::col` in column_id, else its name."""
    cid = model_col.get("column_id", "") or ""
    return (cid.split("::")[-1] if "::" in cid else model_col.get("name", "") or "").lower()


def _iter_strings(obj):
    """Yield every string anywhere in a nested structure. The join condition key `on` parses as
    the boolean True in YAML 1.1, so we can't rely on key names — scan values."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def _expr_refs(obj, targets, display_targets):
    """True if any `[table::Col]` / `[Display Name]` / `[formula_<Name>]` reference inside obj hits
    a target. Formula-to-formula refs carry a `formula_` id prefix, so also match after stripping
    it."""
    for expr in _iter_strings(obj):
        for inner in _BRACKET_REF.findall(expr):
            tail = inner.split("::")[-1].strip().lower()
            whole = inner.strip().lower()
            if tail in targets or whole in display_targets:
                return True
            for cand in (tail, whole):
                if cand.startswith("formula_") and (cand[len("formula_"):] in targets
                                                     or cand[len("formula_"):] in display_targets):
                    return True
    return False


def _resolve_display_names(docs, targets):
    """Display names of model/worksheet columns whose physical name is in `targets`, so leaf
    answers/liveboards (which reference the column by its model display name) can be matched."""
    disp = set()
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            for c in node.get("columns", []) or []:
                if _col_name(c) in targets:
                    nm = (c.get("name") or "").strip().lower()
                    if nm:
                        disp.add(nm)
    return disp


def column_usage(items, column):
    """Column-PRECISE attribution: which objects in `items` actually reference `column`, and where.
    `items` should include the model(s) so leaf answers/liveboards resolve the column's display
    name. Returns [{name, kind, where:[...]}], one entry per referencing object (objects that only
    touch the table via OTHER columns are excluded)."""
    targets = {column.lower()}
    docs = [_doc(it) for it in items]
    display_targets = _resolve_display_names(docs, targets)
    match_set = targets | display_targets

    out = []
    for doc in docs:
        where = []
        # model / worksheet: the column itself, joins, formulas
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            if any(_col_name(c) in targets for c in node.get("columns", []) or []):
                where.append("column")
            for mt in (node.get("model_tables") or node.get("tables") or []):
                for j in mt.get("joins", []) or []:
                    if _expr_refs(j, targets, display_targets):
                        where.append(f"join {mt.get('name','')}->{j.get('with','')}".strip())
            for fdef in node.get("formulas", []) or []:
                if _expr_refs(fdef, targets, display_targets):
                    where.append(f"formula:{fdef.get('name','?')}")
        # liveboard: which vizzes reference it
        lb = doc.get("liveboard")
        if lb:
            for viz in lb.get("visualizations", []) or []:
                ac = [(c.get("name", "") or "").strip().lower()
                      for c in viz.get("answer", {}).get("answer_columns", [])]
                if any(a in match_set for a in ac) or _expr_refs(viz, targets, display_targets):
                    where.append(f"viz:{viz.get('id') or viz.get('viz_id') or 'viz'}")
        # saved answer
        ans = doc.get("answer")
        if ans:
            ac = [(c.get("name", "") or "").strip().lower() for c in ans.get("answer_columns", [])]
            if any(a in match_set for a in ac) or _expr_refs(ans, targets, display_targets):
                where.append("uses column")

        if where:
            typ = tml_type(doc) or "object"
            name = (doc.get(typ) or {}).get("name", "") if isinstance(doc.get(typ), dict) else ""
            seen, w = set(), []
            for x in where:
                if x not in seen:
                    seen.add(x)
                    w.append(x)
            out.append({"name": name or "(unnamed)", "kind": typ, "where": w})
    return out


def column_dependents(items, columns):
    """Read-only preview of what references the given columns across the release set, so a reviewer
    sees the blast radius BEFORE choosing to drop. Columns are matched on the physical column name
    (column_id tail) and the resolved model display name.

    Returns {model_columns, joins, formulas, vizzes}. Joins and formulas reference columns by
    `[table::Col]` / `[Display Name]`, so dropping a column they use leaves a dangling reference —
    which is why drop_columns cascades them out rather than leaving them behind."""
    targets = {c.split("::")[-1].lower() if "::" in c else c.lower() for c in columns}
    docs = [_doc(it) for it in items]
    display_targets = _resolve_display_names(docs, targets)
    match_set = targets | display_targets

    deps = {"model_columns": [], "joins": [], "formulas": [], "vizzes": []}
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            for c in node.get("columns", []) or []:
                if _col_name(c) in targets:
                    deps["model_columns"].append(c.get("name") or _col_name(c))
            for mt in (node.get("model_tables") or node.get("tables") or []):
                for j in mt.get("joins", []) or []:
                    if _expr_refs(j, targets, display_targets):
                        deps["joins"].append(j.get("name") or f"{mt.get('name','')} -> {j.get('with','')}")
            for f in node.get("formulas", []) or []:
                if _expr_refs(f, targets, display_targets):
                    deps["formulas"].append(f.get("name") or "(unnamed formula)")
        lb = doc.get("liveboard")
        if lb:
            for viz in lb.get("visualizations", []) or []:
                acols = [(c.get("name", "") or "").lower()
                         for c in viz.get("answer", {}).get("answer_columns", [])]
                if any(a in match_set for a in acols) or _expr_refs(viz, targets, display_targets):
                    deps["vizzes"].append(viz.get("id") or viz.get("viz_id") or "(viz)")
        ans = doc.get("answer")
        if ans:
            acols = [(c.get("name", "") or "").lower() for c in ans.get("answer_columns", [])]
            if any(a in match_set for a in acols) or _expr_refs(ans, targets, display_targets):
                deps["vizzes"].append(ans.get("name") or "(answer)")
    for k in deps:                                   # dedupe, preserve order
        seen, out = set(), []
        for v in deps[k]:
            if v not in seen:
                seen.add(v)
                out.append(v)
        deps[k] = out
    return deps


def _refs_any(obj, removed, removed_qual=None):
    """True if any `[table::Col]` / `[Display]` / `[Formula Name]` / `[formula_<Name>]` reference
    inside obj hits a target.

    `removed` (bare names, lowercased) matches by NAME regardless of table — used for display
    names, formula names, and bare-name column drops. `removed_qual` (set of (table, col), both
    lowercased) matches ONLY a `[table::col]` reference to that exact table — so a warehouse-missing
    column dropped from ONE table doesn't take out same-named columns on other tables.

    Formula-to-formula refs carry the `formula_` id prefix, so a ref also matches after stripping
    a leading `formula_`."""
    removed_qual = removed_qual or set()
    for expr in _iter_strings(obj):
        for inner in _BRACKET_REF.findall(expr):
            tail = inner.split("::")[-1].strip().lower()
            head = inner.split("::")[0].strip().lower() if "::" in inner else None
            if head is not None and (head, tail) in removed_qual:
                return True
            for cand in (tail, inner.strip().lower()):
                if cand in removed:
                    return True
                if cand.startswith("formula_") and cand[len("formula_"):] in removed:
                    return True
    return False


def _viz_refs(viz, removed, removed_qual=None):
    """A liveboard viz references a removed name via its answer_columns or any inner expr."""
    for c in viz.get("answer", {}).get("answer_columns", []) or []:
        if (c.get("name", "") or "").strip().lower() in removed:
            return True
    return _refs_any(viz, removed, removed_qual)


def drop_columns(items, columns):
    """Remove the named columns from every model/table AND cascade-remove everything that depended
    on them — joins and formulas whose expression references a dropped column, then (transitively)
    any formula/viz that referenced THOSE formulas, and any liveboard viz that references a removed
    column/formula. Layout tiles for removed vizzes are pruned too.

    `columns` may be a bare name (display or db_column_name — matched across ALL tables) OR a
    qualified `table::col` (matched ONLY on that table, so a warehouse-missing column dropped from
    one table doesn't take same-named columns off other tables). The `drop` token on a
    missing_warehouse_column finding is exactly this qualified form.

    Non-destructive: works on deep copies, so the caller's docs are untouched. Returns
    (new_docs, manifest) where new_docs are the modified TML dicts (json.dumps them back into
    import_tml) and manifest = {columns, joins, formulas:[names], vizzes, column_names, join_names}.
    """
    # Split targets: bare names vs table-qualified (table, col).
    targets = set()          # bare column names (lowercased)
    removed_qual = set()     # (table_lower, col_lower) — scoped to that table only
    for c in columns:
        cl = (c or "").strip().lower()
        if "::" in cl:
            tbl, col = cl.split("::", 1)
            removed_qual.add((tbl.strip(), col.strip()))
        else:
            targets.add(cl)

    docs = [copy.deepcopy(_doc(it)) for it in items]     # never mutate the caller's docs
    # A column dropped by physical name is referenced downstream by its model DISPLAY name, so seed
    # the "removed reference names" with both.
    removed = set(targets) | _resolve_display_names(docs, targets)

    man = {"columns": 0, "joins": 0, "formulas": [], "vizzes": 0,
           "column_names": [], "join_names": []}    # names: so a drop can be itemized, not just counted
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            # Formulas AND the columns that surface them cascade together to a fixpoint:
            #  - a formula referencing a removed name is removed (its name joins `removed`),
            #  - a model column whose column_id is `formula_<name>` for a removed formula is ALSO
            #    removed — otherwise it dangles as an "invalid formula ID" on import,
            #  - removing that column can in turn orphan another formula, so we re-scan.
            changed = True
            while changed:
                changed = False
                if node.get("formulas"):
                    keep = []
                    for fdef in node["formulas"]:
                        nm = (fdef.get("name", "") or "").strip().lower()
                        if _refs_any(fdef, removed, removed_qual) or nm in removed:
                            man["formulas"].append(fdef.get("name", "?"))
                            if nm and nm not in removed:
                                removed.add(nm); changed = True
                        else:
                            keep.append(fdef)
                    node["formulas"] = keep
                if node.get("columns") is not None:
                    keep = []
                    for c in node["columns"]:
                        cid = (c.get("column_id", "") or "").strip().lower()
                        dn = (c.get("name", "") or "").strip().lower()
                        # A formula-surfacing column may carry column_id `formula_<name>` OR no
                        # column_id at all (linked to its formula purely by NAME). Either way, once
                        # the formula is gone the column must go too, or it dangles as an "invalid
                        # formula ID" on import — so also drop a column whose NAME matches a removed
                        # formula/column.
                        surfaces_removed_formula = (
                            cid.startswith("formula_") and cid[len("formula_"):] in removed)
                        # qualified: model column_id is `table::col` — drop only if THAT table's
                        # column is targeted (not a same-named column on another table).
                        _qual_hit = ("::" in cid
                                     and (cid.split("::")[0], cid.split("::")[-1]) in removed_qual)
                        if (_col_name(c) in targets or surfaces_removed_formula or dn in removed
                                or _qual_hit):
                            man["columns"] += 1
                            man["column_names"].append(c.get("name") or _col_name(c))
                            if dn and dn not in removed:
                                removed.add(dn); changed = True   # re-scan: vizzes/formulas on it
                        else:
                            keep.append(c)
                    node["columns"] = keep
            # Joins whose `on` condition references a removed name.
            for mt in (node.get("model_tables") or node.get("tables") or []):
                if mt.get("joins"):
                    _kept_j = [j for j in mt["joins"] if not _refs_any(j, removed, removed_qual)]
                    for j in mt["joins"]:
                        if j not in _kept_j:
                            man["join_names"].append(
                                j.get("name") or f"{mt.get('name','')} -> {j.get('with','')}")
                    man["joins"] += len(mt["joins"]) - len(_kept_j)
                    mt["joins"] = _kept_j

        t = doc.get("table")
        if t and t.get("columns") is not None:
            _tl = (t.get("name", "") or "").strip().lower()

            def _drop_phys(c):
                nm = (c.get("name", "") or "").lower()
                dbn = (c.get("db_column_name", "") or "").lower()
                if nm in targets or dbn in targets:
                    return True   # bare name — any table
                return (_tl, nm) in removed_qual or (_tl, dbn) in removed_qual   # qualified — this table
            _keep_t = [c for c in t["columns"] if not _drop_phys(c)]
            for c in t["columns"]:
                if c not in _keep_t:
                    man["column_names"].append(
                        f"{t.get('name','')}.{c.get('name') or c.get('db_column_name','')}")
            man["columns"] += len(t["columns"]) - len(_keep_t)
            t["columns"] = _keep_t

        lb = doc.get("liveboard")
        if lb and lb.get("visualizations") is not None:
            kept, kept_ids = [], set()
            for viz in lb["visualizations"]:
                if _viz_refs(viz, removed, removed_qual):
                    man["vizzes"] += 1
                else:
                    kept.append(viz)
                    kept_ids.add(str(viz.get("id") or viz.get("viz_id") or ""))
            lb["visualizations"] = kept
            layout = lb.get("layout") or {}

            def _prune(tiles):
                return [ti for ti in tiles if str(ti.get("visualization_id", "")) in kept_ids]
            if isinstance(layout.get("tiles"), list):
                layout["tiles"] = _prune(layout["tiles"])
            if isinstance(layout.get("tabs"), list):
                for tab in layout["tabs"]:
                    if isinstance(tab.get("tiles"), list):
                        tab["tiles"] = _prune(tab["tiles"])

    return docs, man


def column_drop_cascade(items, columns):
    """Dry-run: what drop_columns(items, columns) WOULD remove, for a pre-confirm preview. Returns
    the same manifest dict without mutating anything (drop_columns already works on deep copies)."""
    _out, man = drop_columns(items, columns)
    return man
