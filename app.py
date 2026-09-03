"""
Inter-Org Promotion - Streamlit UI.

Configure everything in the Setup tab (host, default auth, then every org in one place:
its role, its own credential or the default, and for targets the connection + db/schema)
with live org/connection discovery - no .env or orgs.json hand-editing. Then run the flow:
Snapshot -> Variables -> Deploy.

Run:  streamlit run app.py
"""
import os

from dotenv import load_dotenv
import pandas as pd
import streamlit as st

from services import pipeline, ui_setup
from services.reconcile import reconcile
from services import nl_instructions, feedback_replace

_TYF = {"liveboard": "Liveboard", "answer": "Answer", "model": "Model", "worksheet": "Model",
        "table": "Table", "view": "View", "sql_view": "SQL View"}

# ThoughtSpot system/Usage-Analytics content can't be exported (the snapshot aborts on it) and
# always exists in the target, so it must never enter a promotion set. Detected by AUTHOR:
# authorName == "system" is the reliable signal (verified on-cluster over 400 objects) — customer
# content never has it, while name matching both over- and under-matches (a user object named
# "TS: ..." vs a system object named "How users are searching answers"). The name set is only a
# belt-and-suspenders for the known blockers in case author is ever missing.
_SYSTEM_NAMES = {"User Adoption", "Object Usage", "Credit Usage", "Billable Query Stats",
                 "Performance Tracking", "Provisioned Users", "ATLAS_NODE_COUNT",
                 "Credits Purchased", "TS BI: Server", "TS: BI Server"}


def _is_system(asset: dict) -> bool:
    """True for TS system content, by author (authorName == 'system'), with the known
    unexportable names as a fallback. Takes the asset dict from list_source_assets."""
    if (asset.get("author") or "").strip().lower() == "system":
        return True
    return (asset.get("name") or "").strip() in _SYSTEM_NAMES


def _select_set(dp, namemap, key, src=None):
    """Selectable resolved-set editor used by every scope (Pick assets / By tag / By collection).
    dp = {groups:[{root_id, objects:[{name,type,obj_id,guid}]}], failures} from preview_dependencies;
    namemap = {root_id: display name}. Renders the deduped set (roots + dependencies) with an
    Include checkbox (default on) + a 'Used by' column, and returns the guids the user kept — so the
    snapshot promotes exactly that set. Also renders the obj_id alignment vs target inline (right on
    this selection view) for the kept objects. Returns [] when nothing is resolved/selected."""
    if not dp:
        return []
    if dp.get("error"):
        st.warning(f"Couldn't resolve dependencies - {dp['error']}")
        return []
    groups = dp.get("groups") or []
    flat = {}
    for g in groups:
        rn = namemap.get(g["root_id"], g["root_id"])
        for o in g["objects"]:
            gid = o.get("guid") or o.get("obj_id")
            e = flat.setdefault(gid, {"Name": o["name"], "Type": _TYF.get(o["type"], o["type"]),
                                      "rawtype": o["type"], "obj_id": o["obj_id"], "guid": gid,
                                      "_used": set()})
            e["_used"].add(rn)
    if dp.get("failures"):
        st.error(f"{len(dp['failures'])} object(s) can't be exported (access): "
                 + ", ".join(f"{f.get('type')} '{f.get('name')}'" for f in dp["failures"]))
    if not flat:
        return []
    st.caption(f"**{len(flat)} object(s) resolved** — untick any you don't want to promote (e.g. a "
               "table that already exists in the target; it resolves there by obj_id). You can **edit "
               "the obj_id** cell to set the source's obj_id, then apply it below. Note: dropping a "
               "model/table that a kept object needs will fail on import unless the target has it.")
    rows = [{"Include": True, "Name": v["Name"], "Type": v["Type"], "obj_id": v["obj_id"],
             "Used by": ", ".join(sorted(v["_used"])), "guid": v["guid"]} for v in flat.values()]
    ed = st.data_editor(
        pd.DataFrame(rows), hide_index=True, use_container_width=True, key=key,
        column_config={"Include": st.column_config.CheckboxColumn("Include"),
                       "Name": st.column_config.TextColumn(disabled=True),
                       "Type": st.column_config.TextColumn(disabled=True),
                       "obj_id": st.column_config.TextColumn(
                           "obj_id", help="Edit to rename this object's obj_id on the SOURCE org, "
                           "then click Apply below."),
                       "Used by": st.column_config.TextColumn(disabled=True),
                       "guid": None})
    recs = ed.to_dict("records")
    kept = [r["guid"] for r in recs if r.get("Include")]
    # detect edited obj_ids (grid value vs what's actually on the source) → offer to write to SOURCE
    edits = {}
    for r in recs:
        g = r["guid"]
        old = flat.get(g, {}).get("obj_id", "")
        new = (r.get("obj_id") or "").strip()
        if new and old and new != old:
            edits[old] = new
    if edits:
        st.warning(f"{len(edits)} obj_id edit(s) pending: "
                   + "; ".join(f"`{o}` → `{n}`" for o, n in edits.items()))
        if st.button("Apply obj_id edits to SOURCE org", key=f"{key}_edit_apply", type="primary"):
            with st.spinner("Rewriting obj_ids on the source org…"):
                try:
                    res = pipeline.align_source_obj_ids(edits, src)
                    if res.get("errors"):
                        st.error("Some rewrites failed: " + "; ".join(res["errors"]))
                    if res.get("done"):
                        st.success(f"Renamed {len(res['done'])} obj_id(s) on the source org. "
                                   "Re-resolve (change and restore the selection, or re-run the scope) "
                                   "to see the new values, then Snapshot.")
                        # drop cached resolution so the next render re-reads the new obj_ids
                        for ck in ("deps_key", "tag_deps_key", "deps_preview", "tag_deps", "coll_deps"):
                            ss = st.session_state
                            ss.pop(ck, None)
                except Exception as e:
                    st.error(f"Apply failed: {e}")
    sel_objs = [{"name": flat[g]["Name"], "type": flat[g]["rawtype"], "obj_id": flat[g]["obj_id"]}
                for g in kept if g in flat]
    _align_section(sel_objs, key, src)
    return kept


def _align_section(objects, key, src=None):
    """obj_id alignment vs a target, rendered inline on the selection view (before Snapshot).
    For the kept `objects` [{name,type(tml),obj_id}] it checks each obj_id against a chosen target:
    in_place (updates in place), would_duplicate (a differently-id'd twin exists → creates a dup), or
    new. would_duplicate rows can be aligned ON THE SOURCE so the next snapshot lands in place."""
    tg = pipeline._targets()
    if not tg or not objects:
        return
    _open = f"{key}_aopen"                              # keep this panel open across reruns
    with st.expander(f"🔗 obj_id alignment vs target ({len(objects)} object(s) selected)",
                     expanded=st.session_state.get(_open, False)):   # collapse-on-click was the bug
        st.caption("Does each selected object's obj_id already exist in the target? If a same-named "
                   "object exists under a DIFFERENT obj_id, promoting would create a duplicate — align "
                   "the source's obj_id to the target's first.")
        atgt = st.selectbox("Target to check against", list(tg),
                            format_func=lambda k: tg[k].get("name", k), key=f"{key}_atgt")
        if st.button("Check obj_id alignment", key=f"{key}_abtn"):
            st.session_state[_open] = True             # stay open when the rerun re-renders the expander
            with st.spinner("Checking obj_ids in target…"):
                try:
                    st.session_state[f"{key}_ares"] = pipeline.check_alignment_objects(objects, atgt)
                    st.session_state[f"{key}_afor"] = atgt
                    st.session_state.pop(f"{key}_aerr", None)
                except Exception as e:
                    st.session_state.pop(f"{key}_ares", None)
                    st.session_state[f"{key}_aerr"] = str(e)
            st.rerun()                                 # re-render expanded, with the result/error shown
        if st.session_state.get(f"{key}_aerr"):
            st.error(f"Alignment check failed: {st.session_state[f'{key}_aerr']}")
        ar = st.session_state.get(f"{key}_ares")
        if ar and st.session_state.get(f"{key}_afor") == atgt:
            _vlabel = {"in_place": "✅ in place", "would_duplicate": "⚠️ would DUPLICATE", "new": "🆕 new"}
            disp = [{"Name": r["name"], "Type": _TYF.get(r["type"], r["type"]),
                     "source obj_id": r["obj_id"], "verdict": _vlabel.get(r["verdict"], r["verdict"]),
                     "target obj_id": r.get("target_obj_id", "")} for r in ar.get("rows", [])]
            st.dataframe(pd.DataFrame(disp), hide_index=True, use_container_width=True)
            dups = ar.get("suggest") or {}
            if dups:
                st.warning(f"{len(dups)} object(s) would DUPLICATE in the target. Align them on the "
                           "SOURCE org so the next snapshot updates in place:")
                if st.button("Align these on the SOURCE org", key=f"{key}_aapply", type="primary"):
                    with st.spinner("Rewriting obj_ids on the source org…"):
                        try:
                            res = pipeline.align_source_obj_ids(dups, src)
                            if res.get("errors"):
                                st.error("Some rewrites failed: " + "; ".join(res["errors"]))
                            if res.get("done"):
                                st.success(f"Aligned {len(res['done'])} obj_id(s) on the source. "
                                           "Re-check or re-resolve, then Snapshot.")
                            st.session_state.pop(f"{key}_ares", None)
                        except Exception as e:
                            st.error(f"Align failed: {e}")
            else:
                st.success("No duplicates — every selected object aligns in place or is new.")


def _refresh_snap_manifest():
    """Re-read the release from Git into ss['snap_result'] after an obj_id rewrite."""
    try:
        area = pipeline.git().read_area(pipeline._release_area(), ref=pipeline._branch())
        objs = []
        for fn, txt in area.items():
            if not fn.endswith(".tml"):
                continue
            d = pipeline.load_tml(txt)
            t = pipeline.tml_type(d) or "object"
            objs.append({"file": fn, "name": (d.get(t, {}) or {}).get("name", ""),
                         "type": t, "obj_id": d.get("obj_id", "")})
        st.session_state["snap_result"]["objects"] = objs
        st.session_state["snap_result"]["files"] = [o["file"] for o in objs]
    except Exception:
        pass


def _resnapshot():
    """Re-run the last snapshot (after source obj_ids changed) so the release picks up the new ids."""
    si = st.session_state.get("snap_inputs") or {}
    st.session_state["snap_result"] = pipeline.snapshot(
        source_org=si.get("src") or None, from_seed=si.get("from_seed", False),
        object_ids=si.get("object_ids") or None, include_dependencies=si.get("incl", True))

load_dotenv()

st.set_page_config(page_title="Inter-Org Promotion", layout="wide")
st.title("ThoughtSpot Inter-Org Promotion")


def _store_caption() -> str:
    if os.environ.get("GIT_LOCAL_DIR"):
        return f"Git store: local folder `{os.environ['GIT_LOCAL_DIR']}`"
    if os.environ.get("GITHUB_REPO"):
        return f"Git store: github.com/{os.environ['GITHUB_REPO']}"
    return "Git store: not configured yet - set it in the **Setup** tab"


st.caption(_store_caption() + "  ·  one parameterized `release/`; each org's values resolve "
           "`${ts_db}`/`${ts_schema}`; obj_id is the cross-org identity.")

tabs = st.tabs(["0 · Setup", "1 · Snapshot", "2 · Variables", "3 · Deploy", "Repo state"])

# ── 0 · setup ──────────────────────────────────────────────────────────────────────
with tabs[0]:
    st.subheader("Configure everything here - no file editing")
    ss = st.session_state

    st.markdown("### Step 1 - Primary connection  (prerequisite)")
    st.caption("A trusted-auth secret (with an admin user) or an admin login for the **Primary** "
               "org. Required first, and used for every org: it discovers all the orgs on the "
               "cluster and mints a token per org as needed, so no per-org credentials are needed. "
               "Step 2 unlocks once this connects.")
    host = st.text_input("Host", value=os.environ.get("TS_HOST", ""),
                         placeholder="https://your-instance.thoughtspot.cloud")
    _auth_opts = ["Secret key (trusted auth)", "Bearer token", "Username + password"]
    _auth_idx = (1 if os.environ.get("TS_TOKEN") and not os.environ.get("TS_SECRET_KEY")
                 else 2 if os.environ.get("TS_PASSWORD") and not os.environ.get("TS_SECRET_KEY")
                 else 0)
    auth = st.radio("Primary auth method", _auth_opts, index=_auth_idx,
                    horizontal=True,
                    help="Secret key: SSO/MFA orgs, where basic login is blocked; mints a "
                         "short-lived per-org token.  Bearer token: you already hold one "
                         "(it is bound to the org it was minted for).  Username + password: "
                         "local (non-SSO) accounts only.")
    user = secret = token = target_token = password = ""
    if auth.startswith("Secret"):
        user = st.text_input("Username (token is minted for this user)", value=os.environ.get("TS_USER", ""))
        secret = st.text_input("Trusted-auth secret key", value=os.environ.get("TS_SECRET_KEY", ""), type="password")
    elif auth.startswith("Bearer"):
        st.caption("Bearer tokens are **org-bound**, so provide one per side: the **source** token "
                   "(where you snapshot from) and the **target** token (where you deploy to). "
                   "A trusted-auth secret would cover both with one credential.")
        bc1, bc2 = st.columns(2)
        with bc1:
            token = st.text_input("Source bearer token (snapshot)", value=os.environ.get("TS_TOKEN", ""),
                                  type="password", key="src_bearer")
        with bc2:
            target_token = st.text_input("Target bearer token (deploy)", value=os.environ.get("TS_TOKEN_TARGET", ""),
                                         type="password", key="tgt_bearer")
    else:
        user = st.text_input("Username", value=os.environ.get("TS_USER", ""))
        password = st.text_input("Password", value=os.environ.get("TS_PASSWORD", ""), type="password")
    primary_org = st.text_input("Primary org id (the token connects here to list the orgs, and "
                                "it manages variables unless an org below is tagged 'variables')",
                                value=os.environ.get("TS_ORG_PRIMARY", "0"))

    with st.expander("Network / SSL (only if behind a corporate proxy)"):
        st.caption("Use this if connecting fails with CERTIFICATE_VERIFY_FAILED. A TLS-inspection "
                   "proxy re-signs HTTPS with an internal CA that Python doesn't trust by default. "
                   "Point at your corporate CA bundle (recommended), or disable verification as a "
                   "last resort on a trusted network.")
        ca_bundle = st.text_input("CA bundle path (.pem)", value=os.environ.get("TS_CA_BUNDLE", ""),
                                  placeholder="C:\\path\\to\\corporate-ca.pem")
        disable_verify = st.checkbox(
            "Disable SSL verification (insecure - trusted corporate proxy only)",
            value=os.environ.get("TS_VERIFY_SSL", "").strip().lower() in ("0", "false", "no", "off"))
    verify_ssl = not disable_verify

    def _cfg() -> dict:
        return {"host": host.rstrip("/"), "user": user, "secret": secret, "token": token,
                "target_token": target_token,
                "password": password, "primary_org": primary_org,
                "ca_bundle": ca_bundle, "verify_ssl": verify_ssl,
                "tag": ss.get("tag", ""), "resolve_local": ss.get("resolve_local", True),
                "git_local_dir": ss.get("git_local_dir", ""),
                "github_repo": ss.get("github_repo", ""), "github_token": ss.get("github_token", ""),
                "git_branch": ss.get("git_branch", ""), "git_base_branch": ss.get("git_base_branch", ""),
                "github_api_url": ss.get("github_api_url", ""), "git_base_path": ss.get("git_base_path", "")}

    if st.button("Test connection & load orgs", type="primary"):
        try:
            orgs, note = ui_setup.connect(_cfg())
            ss["connected"] = True
            ss["orgs"] = orgs
            ss.setdefault("orgs_cfg", ui_setup.load_orgs_config())
            if note:
                st.warning(f"Connected, but {note}.")
            else:
                st.success(f"Connected. Loaded {len(orgs)} orgs. Step 2 is unlocked below.")
        except Exception as e:
            ss.pop("connected", None); ss.pop("orgs", None)
            st.error(f"Connection failed - {type(e).__name__}: {str(e)[:300]}")

    if ss.get("connected"):
        ss.setdefault("orgs", [])
        ss.setdefault("orgs_cfg", {})

        st.markdown("**Orgs in play**")
        st.caption("The orgs your credential can reach — auto-loaded on connect (admins get all "
                   "cluster orgs; otherwise the orgs you belong to). Remove any you won't use in "
                   "this promotion.")
        if ss["orgs"]:
            for _i, _n in list(ss["orgs"]):
                rc1, rc2 = st.columns([8, 1])
                rc1.write(f"• {_n}  (`{_i}`)")
                if rc2.button("Remove", key=f"rm_org_{_i}"):
                    ss["orgs"] = [(x, y) for x, y in ss["orgs"] if x != _i]
                    st.rerun()
        else:
            st.info("No orgs loaded — your credential couldn't enumerate any orgs. Reconnect in "
                    "Step 1 with a credential that has access to the source/target orgs.")

        orgs = ss["orgs"]
        id2name = {i: n for i, n in orgs}
        ids = [i for i, _ in orgs]

        # ── Git store ──
        st.markdown("**Git store**")
        # Default the radio to the mode that's actually configured, so opening Setup never silently
        # flips a GitHub-configured store onto an empty "Local folder" (a Save would then wipe it).
        _gm_idx = 1 if (os.environ.get("GITHUB_REPO") and not os.environ.get("GIT_LOCAL_DIR")) else 0
        gitmode = st.radio("Where to store the release", ["Local folder", "GitHub repo"],
                           horizontal=True, index=_gm_idx)
        if gitmode == "Local folder":
            ss["git_local_dir"] = st.text_input("Local folder path (any folder, e.g. inside a git clone - no GitHub token needed)",
                                                value=ss.get("git_local_dir", "") or os.environ.get("GIT_LOCAL_DIR", ""))
            ss["github_repo"] = ss["github_token"] = ss["git_branch"] = ss["git_base_branch"] = ss["github_api_url"] = ""
        else:
            ss["github_repo"] = st.text_input("GitHub repo (owner/name) - the RELEASE repo, never the source repo",
                                              value=ss.get("github_repo", "") or os.environ.get("GITHUB_REPO", ""))
            ss["github_token"] = st.text_input("GitHub token (needs: create branch + open PR - `repo` scope, or fine-grained Contents+Pull requests write)",
                                               value=ss.get("github_token", "") or os.environ.get("GITHUB_TOKEN", ""), type="password")
            ss["github_api_url"] = st.text_input(
                "API base URL (blank = github.com; GitHub Enterprise Server: https://<host>/api/v3)",
                value=ss.get("github_api_url", "") or os.environ.get("GITHUB_API_URL", ""))
            ss["git_base_branch"] = st.text_input(
                "Base branch - the release branch is cut FROM this and the PR opens INTO it (default main; set to e.g. develop)",
                value=ss.get("git_base_branch", "") or os.environ.get("GIT_BASE_BRANCH", "main"))
            ss["git_branch"] = st.text_input(
                "Release branch - commits here and opens a PR into the base branch (use when the base is protected). "
                "Blank = commit straight to the base branch.",
                value=ss.get("git_branch", "") or os.environ.get("GIT_BRANCH", "ts-release"))
            ss["git_local_dir"] = ""

        ss["git_base_path"] = st.text_input(
            "Subfolder (optional) - nest the release under this path in the repo/folder (e.g. thoughtspot). Blank = root.",
            value=ss.get("git_base_path", "") or os.environ.get("GIT_BASE_PATH", ""))

        st.markdown("**Options**")
        ss["resolve_local"] = st.checkbox(
            "Resolve variables locally (use when the Variables feature isn't enabled on the cluster)",
            value=ss.get("resolve_local", True))
        ss["tag"] = st.text_input("Release tag (empty = ALL objects in the source org)",
                                  value=ss.get("tag", "") or os.environ.get("TS_RELEASE_TAG", ""))

        # ── Orgs in this promotion: one block per org, all visible together ──
        st.divider()
        st.markdown("### Step 2 - Orgs in this promotion")
        st.caption("Pick every org involved, then set each one's role and - for targets - the "
                   "connection + db/schema. All orgs use the Step 1 primary credential.")

        participating = st.multiselect(
            "Orgs involved", ids, default=[o for o in ids if o in ss["orgs_cfg"]],
            format_func=lambda i: f"{id2name.get(i, i)}  ({i})", key="participating")

        new_cfg = {}
        for oid in participating:
            prev = ss["orgs_cfg"].get(oid, {})
            with st.expander(f"{id2name.get(oid, oid)}  ({oid})", expanded=True):
                roles = st.multiselect(
                    "Role(s)", ["source", "variables", "target"], default=prev.get("role", []),
                    key=f"role_{oid}",
                    help="source = snapshot FROM;  variables = manages the TABLE_MAPPING "
                         "variables (the Primary org);  target = deploy TO")
                conn_name = prev.get("connection", "")
                vals = prev.get("values", {}) or {}
                ts_db, ts_schema, conn_id = vals.get("ts_db", ""), vals.get("ts_schema", ""), None
                if "target" in roles:
                    st.markdown("_Target binding_ (where the data lives in this org)")
                    b1, b2 = st.columns([3, 2])
                    with b2:
                        if st.button("Load connections", key=f"loadconn_{oid}"):
                            try:
                                ss.setdefault("conns", {})[oid] = ui_setup.list_connections(_cfg(), oid)
                            except Exception as e:
                                st.error(f"Couldn't list connections - {str(e)[:200]}")
                    conns = ss.get("conns", {}).get(oid, [])
                    with b1:
                        if conns:
                            names = [n for _, n in conns]
                            conn_name = st.selectbox("Connection (in this org)", names,
                                                     index=names.index(conn_name) if conn_name in names else 0, key=f"conn_{oid}")
                            conn_id = next((i for i, n in conns if n == conn_name), None)
                        else:
                            conn_name = st.text_input("Connection name (in this org)", value=conn_name, key=f"connm_{oid}")
                    d1, d2, d3 = st.columns([2, 2, 1])
                    with d1:
                        ts_db = st.text_input("Database (ts_db)", value=ts_db, key=f"db_{oid}")
                    with d2:
                        ts_schema = st.text_input("Schema (ts_schema)", value=ts_schema, key=f"schema_{oid}")
                    with d3:
                        if st.button("Fetch dbs", key=f"fetch_{oid}"):
                            dbs = ui_setup.fetch_databases(_cfg(), oid, conn_id) if conn_id else []
                            st.info("DBs: " + (", ".join(dbs) if dbs else "(none returned - type it; read it off the connection's Edit page)"))

                new_cfg[oid] = {"name": id2name.get(oid, str(oid)), "role": roles,
                                "connection": conn_name, "values": {"ts_db": ts_db, "ts_schema": ts_schema}}

        ss["orgs_cfg"] = new_cfg          # live form state (source of truth for Save)

        if new_cfg:
            st.markdown("**Summary**")
            st.table([{"org_id": oid, "name": r.get("name", oid),
                       "role": ", ".join(r.get("role", [])) or "-",
                       "connection": r.get("connection", "") or "-",
                       "ts_db / ts_schema": " / ".join(x for x in [r.get("values", {}).get("ts_db", ""),
                                                                    r.get("values", {}).get("ts_schema", "")] if x) or "-"}
                      for oid, r in new_cfg.items()])

        st.divider()
        if st.button("Save configuration", type="primary"):
            _c = _cfg()
            if not (_c.get("git_local_dir") or _c.get("github_repo")):
                # Guard: never persist a config with no git store - that silently wiped the store
                # before and left Snapshot with nowhere to write.
                st.error("Choose a git store before saving: enter a **local folder path**, or switch to "
                         "**GitHub repo** and enter the repo (+ token), under *Git store* above.")
            else:
                try:
                    p1, p2 = ui_setup.write_config(_c, new_cfg)
                    st.success(f"Saved and live for this session (also written to {p1} and {p2}). "
                               "Use the Snapshot / Variables / Deploy tabs now.")
                except Exception as e:
                    st.error(f"Save failed - {str(e)[:300]}")
    else:
        st.info("Enter host + default auth, then click **Test connection & load orgs**.")

# ── 1 · snapshot ───────────────────────────────────────────────────────────────────
with tabs[1]:
    st.subheader("Snapshot a release into the Git store")
    st.write("Export the source org's objects (or the bundled seed), parameterize "
             "(db/schema → `${...}`, keep obj_id, strip guids), and write `release/`.")
    ss = st.session_state
    from_seed = st.checkbox("Use bundled seed (demo)", value=False)
    src, tag, object_ids, scope, collection = "", None, None, None, None
    if not from_seed:
        orgs = ss.get("orgs")
        if orgs:
            id2name = {i: n for i, n in orgs}
            ids = [i for i, _ in orgs]
            default_src = next((oid for oid, r in ss.get("orgs_cfg", {}).items()
                                if "source" in (r.get("role") or [])), os.environ.get("TS_ORG_SOURCE", ""))
            idx = ids.index(default_src) if default_src in ids else 0
            src = st.selectbox("Source org (snapshot FROM)", ids, index=idx,
                               format_func=lambda i: f"{id2name.get(i, i)}  ({i})")
        else:
            src = st.text_input("Source org id", value=os.environ.get("TS_ORG_SOURCE", ""))
            st.caption("Connect in the **Setup** tab to pick the source org by name.")
        scope = st.radio(
            "What to promote", ["Pick assets", "By tag", "By collection", "All objects in the org"],
            horizontal=True,
            help="Pick assets: choose specific objects; their dependencies (model, tables) "
                 "are pulled in automatically, so you only pick the top-level Liveboard/Answer/Model. "
                 "By collection: promote every asset in a collection, recursing into sub-collections.")
        if scope == "Pick assets":
            if st.button("List assets in the source org"):
                try:
                    ss["snap_assets"] = pipeline.list_source_assets(src or None)
                    ss.pop("asset_pick", None)
                except Exception as e:
                    ss.pop("snap_assets", None)
                    st.error(f"Couldn't list assets - {type(e).__name__}: {str(e)[:200]}")
            # TS system content (authorName == "system") never appears in the picker: it can't be
            # exported and always exists in the target, so it would only block the snapshot.
            _sys_ids = {a["id"] for a in ss.get("snap_assets", []) if _is_system(a)}
            assets = [a for a in ss.get("snap_assets", []) if a["id"] not in _sys_ids]
            if assets:
                _kd = {"LIVEBOARD": "Liveboard", "ANSWER": "Answer", "TABLE": "Table", "MODEL": "Model"}
                _kind = lambda a: a.get("kind") or a.get("type", "")   # fallback for older cache
                f1, f2 = st.columns([3, 2])
                with f1:
                    q = st.text_input("Search by name or tag", key="asset_q",
                                      placeholder="filter the grid…")
                with f2:
                    types_present = sorted({_kind(a) for a in assets})
                    tf = st.multiselect("Filter by type", types_present, default=types_present,
                                        format_func=lambda t: _kd.get(t, t), key="asset_tf")
                ql = (q or "").lower()
                rows = []
                for a in assets:
                    if tf and _kind(a) not in tf:
                        continue
                    tags = ", ".join(a.get("tags", []) or [])
                    if ql and ql not in a.get("name", "").lower() and ql not in tags.lower():
                        continue
                    rows.append({"Name": a.get("name", ""), "Type": _kd.get(_kind(a), _kind(a)),
                                 "Tags": tags, "id": a["id"]})
                df = pd.DataFrame(rows, columns=["Name", "Type", "Tags", "id"])
                sel = st.dataframe(df, hide_index=True, use_container_width=True,
                                   on_select="rerun", selection_mode="multi-row",
                                   column_config={"id": None})  # click headers to sort
                sel_ids = [rows[i]["id"] for i in sel.selection.rows] if rows else []
                a1, a2, a3, a4 = st.columns([2, 2, 2, 3])
                with a1:
                    if st.button(f"Add {len(sel_ids)} to set", disabled=not sel_ids):
                        s = set(ss.get("asset_pick", []))
                        s.update(sel_ids)
                        ss["asset_pick"] = list(s)
                with a2:
                    # symmetric with Add: select rows already in the set, then Remove them
                    if st.button(f"Remove {len(sel_ids)} from set", disabled=not sel_ids):
                        ss["asset_pick"] = [i for i in ss.get("asset_pick", []) if i not in set(sel_ids)]
                with a3:
                    if st.button("Clear set", disabled=not ss.get("asset_pick")):
                        ss["asset_pick"] = []
                # defensively drop any system asset that slipped into the set (e.g. a stale pick
                # from before this filter) — they can't export and would block the snapshot.
                _picked = [i for i in ss.get("asset_pick", []) if i not in _sys_ids]
                if len(_picked) != len(ss.get("asset_pick", [])):
                    ss["asset_pick"] = _picked
                root_ids = ss.get("asset_pick", [])
                with a4:
                    st.caption(f"Picked roots: **{len(root_ids)}**  ·  showing {len(rows)} of {len(assets)}")
                object_ids = []
                if root_ids:
                    key = tuple(sorted(root_ids))
                    if ss.get("deps_key") != key:
                        with st.spinner("Resolving dependencies…"):
                            try:
                                ss["deps_preview"] = pipeline.preview_dependencies(root_ids, src or None)
                            except Exception as e:
                                ss["deps_preview"] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
                        ss["deps_key"] = key
                    object_ids = _select_set(ss.get("deps_preview"),
                                             {a["id"]: a["name"] for a in assets}, "sel_pick", src)
            else:
                st.info("Click **List assets in the source org** to choose objects.")
        elif scope == "By tag":
            if st.button("List tags in the source org"):
                try:
                    ss["src_tags"] = pipeline.list_source_tags(src or None)
                except Exception as e:
                    ss.pop("src_tags", None)
                    st.error(f"Couldn't list tags - {type(e).__name__}: {str(e)[:160]}")
            tags_avail = ss.get("src_tags")
            if tags_avail:
                tag = st.selectbox("Tag", [""] + tags_avail, index=0) or ""
            else:
                tag = st.text_input("Tag", value=os.environ.get("TS_RELEASE_TAG", ""))
                st.caption("Tip: click **List tags in the source org** to pick from what's available.")
            if tag:
                if ss.get("tag_roots_key") != tag:                 # resolve the tag's objects once
                    try:
                        ss["tag_roots"] = pipeline.list_tagged(tag, src or None)
                    except Exception as e:
                        ss["tag_roots"] = []
                        st.error(f"Couldn't resolve tag - {type(e).__name__}: {str(e)[:160]}")
                    ss["tag_roots_key"] = tag
                    ss["tag_pick"] = []                             # reset selection when tag changes
                roots = ss.get("tag_roots", [])
                if not roots:
                    st.warning(f"No objects carry the tag '{tag}' in the source org.")
                else:
                    _tt = {"LIVEBOARD": "Liveboard", "ANSWER": "Answer", "LOGICAL_TABLE": "Table/Model"}
                    st.caption(f"**{len(roots)}** object(s) carry '{tag}'. Select which to promote — "
                               "you don't have to take them all; dependencies come along per pick.")
                    rrows = [{"Name": r["name"], "Type": _tt.get(r["type"], r["type"]),
                              "obj_id": r.get("obj_id", ""), "id": r["id"]} for r in roots]
                    selr = st.dataframe(pd.DataFrame(rrows, columns=["Name", "Type", "obj_id", "id"]),
                                        hide_index=True, use_container_width=True, on_select="rerun",
                                        selection_mode="multi-row", column_config={"id": None})
                    selr_ids = [rrows[i]["id"] for i in selr.selection.rows]
                    b1, b2, b3 = st.columns([2, 2, 3])
                    with b1:
                        if st.button(f"Add {len(selr_ids)} to set", disabled=not selr_ids, key="tag_add"):
                            ss["tag_pick"] = sorted(set(ss.get("tag_pick", [])) | set(selr_ids))
                    with b2:
                        if st.button("Clear set", disabled=not ss.get("tag_pick"), key="tag_clear"):
                            ss["tag_pick"] = []
                    root_ids = ss.get("tag_pick", [])              # tagged roots the user picked
                    with b3:
                        st.caption(f"Picked roots: **{len(root_ids)}** of {len(roots)} tagged asset(s)")
                    object_ids = []
                    if root_ids:
                        key = tuple(sorted(root_ids))
                        if ss.get("tag_deps_key") != key:
                            with st.spinner("Resolving dependencies…"):
                                try:
                                    ss["tag_deps"] = pipeline.preview_dependencies(root_ids, src or None)
                                except Exception as e:
                                    ss["tag_deps"] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
                            ss["tag_deps_key"] = key
                        object_ids = _select_set(ss.get("tag_deps"),
                                                 {r["id"]: r["name"] for r in roots}, "sel_tag", src)
        elif scope == "By collection":
            if st.button("List collections in the source org"):
                try:
                    ss["snap_collections"] = pipeline.list_source_collections(src or None)
                except Exception as e:
                    ss.pop("snap_collections", None)
                    st.error(f"Couldn't list collections - {type(e).__name__}: {str(e)[:200]}")
            cols = ss.get("snap_collections", [])
            if cols:
                c2n = {c["id"]: c["name"] for c in cols}
                collection = st.selectbox(
                    "Collection", [c["id"] for c in cols],
                    format_func=lambda i: f"{c2n.get(i, i)}  ({i[:8]})")
                if st.button("Preview members + dependencies"):
                    with st.spinner("Resolving collection members + dependencies…"):
                        try:
                            members = pipeline.resolve_collection(collection, src or None)
                            ss["coll_namemap"] = {m["id"]: m["name"] for m in members}
                            ss["coll_deps"] = (pipeline.preview_dependencies([m["id"] for m in members], src or None)
                                               if members else {"groups": [], "empty": True})
                        except Exception as e:
                            ss["coll_deps"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                if (ss.get("coll_deps") or {}).get("empty"):
                    st.warning("Collection has no promotable members.")
                else:
                    object_ids = _select_set(ss.get("coll_deps"), ss.get("coll_namemap", {}), "sel_coll", src)
            else:
                st.info("Click **List collections in the source org** to choose one.")

    if st.button("Snapshot", type="primary"):
        _needs_pick = scope in ("Pick assets", "By tag", "By collection")
        if _needs_pick and not object_ids:
            st.warning("Select the objects to promote in the table above (tick the ones you want).")
        elif not (os.environ.get("GIT_LOCAL_DIR") or os.environ.get("GITHUB_REPO")):
            st.error("No git store is configured, so there is nowhere to write the release. In **Setup**, "
                     "under *Where releases are stored*, choose a **GitHub repo + token** or a **local "
                     "folder**, click **Save configuration**, then retry Snapshot.")
        else:
            try:
                with st.status("Snapshotting…", expanded=True) as _snap_status:
                    # Pick assets / By tag / By collection all resolve to a hand-picked set of guids
                    # in `object_ids`; export EXACTLY those (include_dependencies=False) so unticked
                    # objects are omitted. "All objects in the org" leaves object_ids empty -> full org.
                    ss["snap_result"] = pipeline.snapshot(source_org=src or None, from_seed=from_seed,
                                                          object_ids=object_ids or None,
                                                          include_dependencies=(not _needs_pick),
                                                          progress=lambda m: _snap_status.write(m))
                    _snap_status.update(label="Snapshot complete", state="complete")
                    ss["snap_inputs"] = {"src": src, "from_seed": from_seed,
                                         "object_ids": list(object_ids or []),
                                         "incl": (not _needs_pick)}   # to re-snapshot after obj_id changes
            except (Exception, SystemExit) as e:
                # An incomplete export (a dependency the user can't access) or a config error
                # (no git store / token) aborts here instead of writing a thin release or, worse,
                # leaving the status box spinning forever. Show it, don't hide it. SystemExit is
                # caught explicitly because some helpers raise it and it bypasses `except Exception`.
                ss.pop("snap_result", None)
                st.error(f"Snapshot failed - nothing was written.\n\n{e}")

    # Render the snapshot result + obj_id alignment editor (persists across reruns)
    sr = ss.get("snap_result")
    if sr:
        st.success(f"Wrote {len(sr['files'])} file(s) to `release/` @ `{sr['sha'][:8]}`")
        st.write("variables referenced:", sr["variables"])
        if sr.get("source_bindings"):
            st.info("Source tables were bound to (use these as the target **ts_db / ts_schema** "
                    "unless the target differs):  "
                    + ";   ".join(f"`{b['db']} / {b['schema']}`" for b in sr["source_bindings"]))
        _ty2 = {"liveboard": "Liveboard", "answer": "Answer", "model": "Model", "worksheet": "Model",
                "table": "Table", "view": "View", "sql_view": "SQL View"}
        objs = sr.get("objects")
        if objs:
            st.caption("Release contents — edit an **obj_id** to align it with the target org's "
                       "existing object so the promotion updates in place. Applying **changes the "
                       "obj_id on the SOURCE org** (via update-obj-id) and re-snapshots, so source, "
                       "release and target stay consistent. ⚠️ mutates the live source object.")
            base_df = pd.DataFrame([{"Name": o.get("name", ""), "Type": _ty2.get(o.get("type"), o.get("type")),
                                     "obj_id": o.get("obj_id", ""), "file": o.get("file", "")} for o in objs])
            edited = st.data_editor(
                base_df, hide_index=True, use_container_width=True, key="objid_editor",
                column_config={"Name": st.column_config.TextColumn(disabled=True),
                               "Type": st.column_config.TextColumn(disabled=True),
                               "file": st.column_config.TextColumn(disabled=True),
                               "obj_id": st.column_config.TextColumn("obj_id (editable)")})
            mapping = {o["obj_id"]: nv for o, nv in zip(objs, edited["obj_id"].tolist())
                       if o.get("obj_id") and nv and nv != o["obj_id"]}
            if mapping:
                st.caption("Pending source obj_id changes: "
                           + ", ".join(f"`{o}` → `{n}`" for o, n in mapping.items()))
                if st.button("Apply to SOURCE org + re-snapshot"):
                    with st.spinner("Renaming obj_ids on the source + re-snapshotting…"):
                        res = pipeline.align_source_obj_ids(mapping, sr_src := ss.get("snap_inputs", {}).get("src"))
                        if res["errors"]:
                            st.error("Some obj_id changes failed: "
                                     + "; ".join(f"{o}->{n}: {m}" for o, n, m in res["errors"]))
                        if res["done"]:
                            _resnapshot()
                    st.success(f"Changed {len(res['done'])} obj_id(s) on the source and re-snapshotted.")
                    st.rerun()

            # ── obj_id alignment vs a target (auto-suggests which release obj_ids would duplicate) ──
            _atargets = pipeline._targets()
            if _atargets:
                st.markdown("**obj_id alignment vs target** — will each object update in place, or duplicate?")
                atgt = st.selectbox("Check against target", list(_atargets.keys()),
                                    format_func=lambda k: _atargets[k].get("name", k), key="snap_align_tgt")
                if st.button("Check obj_id alignment", key="snap_align_btn"):
                    with st.spinner("Checking obj_ids against the target…"):
                        try:
                            ss["snap_align"] = pipeline.check_target_alignment(atgt)
                            ss["snap_align_for"] = atgt
                        except Exception as e:
                            ss.pop("snap_align", None)
                            st.error(f"Check failed - {type(e).__name__}: {str(e)[:200]}")
                sa = ss.get("snap_align")
                if sa and ss.get("snap_align_for") == atgt:
                    _v = {"in_place": "✅ in place (updates)", "would_duplicate": "⚠️ WOULD DUPLICATE",
                          "new": "🆕 new (created)"}
                    st.dataframe(pd.DataFrame([{"Name": r["name"], "Type": _ty2.get(r["type"], r["type"]),
                                               "source obj_id": r["obj_id"], "verdict": _v.get(r["verdict"], r["verdict"]),
                                               "target obj_id": r["target_obj_id"]} for r in sa["rows"]]),
                                 hide_index=True, use_container_width=True)
                    dups = sa.get("suggest") or {}
                    if dups:
                        st.warning(f"{len(dups)} object(s) would DUPLICATE in the target — align the "
                                   "SOURCE obj_id to the target's (mutates the source object):")
                        if st.button("Align on SOURCE + re-snapshot", key="snap_align_apply"):
                            with st.spinner("Renaming source obj_ids + re-snapshotting…"):
                                res = pipeline.align_source_obj_ids(dups, ss.get("snap_inputs", {}).get("src"))
                                if res["errors"]:
                                    st.error("; ".join(f"{o}->{n}: {m}" for o, n, m in res["errors"]))
                                if res["done"]:
                                    _resnapshot()
                                ss["snap_align"] = pipeline.check_target_alignment(atgt)
                            st.rerun()
                    else:
                        st.success("No duplicates — each object updates in place or is created fresh.")
        else:
            st.table([{"file": f} for f in sr["files"]])
        if sr.get("warnings"):
            st.warning(sr["warnings"])

# ── 2 · variables ──────────────────────────────────────────────────────────────────
with tabs[2]:
    st.subheader("Create variables + assign per-org values")
    st.write("Creates the TABLE_MAPPING variables in the **Primary** org and assigns each "
             "target org its values from the configured targets.")
    targets = pipeline._targets()
    if not targets:
        st.warning("No targets configured - add them in the Setup tab.")
    else:
        st.table([{"name": v.get("name", k), "org_id": v.get("org_id"),
                   "connection": v.get("connection"), "values": v.get("values")} for k, v in targets.items()])
        st.caption("Skip this step if you resolve variables locally (Setup → Resolve variables locally).")
        if st.button("Create + assign", type="primary"):
            values_by_org = {c["org_id"]: c["values"] for c in targets.values() if c.get("values")}
            try:
                with st.spinner("Setting up variables…"):
                    r = pipeline.setup_vars(values_by_org)
                st.success(f"created {r['created'] or '(all existed)'}; {len(r['assigned'])} value(s) assigned")
                st.table(r["assigned"])
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                body = (getattr(getattr(e, "response", None), "text", "") or "") + " " + str(e)
                if code in (401, 403):
                    reason = "Creating variables needs **admin on the Primary org** (this step is admin-gated)."
                elif "Variable Store" in body or "host port" in body:
                    reason = "The **Variables feature isn't enabled** on this cluster (ThoughtSpot Support has to switch it on)."
                else:
                    reason = None
                if reason:
                    st.error(f"{reason}  You can skip this step entirely: in the **Setup** tab tick "
                             "**Resolve variables locally**, then run **Deploy** - the tool bakes each "
                             "org's db/schema in at deploy time instead of using ThoughtSpot Variables.")
                else:
                    st.error(f"Variables step failed - {type(e).__name__}: {str(e)[:300]}")

# ── 3 · deploy ─────────────────────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("Deploy release to a target org")
    st.write("Reads `release/`, remaps the connection to the target org's, imports "
             "(tables first). VALIDATE_ONLY runs first and blocks the import if it fails. Never deletes.")
    targets = pipeline._targets()
    if not targets:
        st.warning("No targets configured - add them in the Setup tab.")
    else:
        ss = st.session_state
        tgt = st.selectbox("Target", list(targets.keys()),
                           format_func=lambda k: f"{targets[k].get('name', k)}  ({k})")
        only = st.checkbox("Validate only (no import)", value=True)

        # ── obj_id alignment check: does the source obj_id land in the target, or duplicate? ──
        if st.button("🔗 Check obj_id alignment vs target"):
            with st.spinner("Checking obj_ids against the target org…"):
                try:
                    ss["align_check"] = pipeline.check_target_alignment(tgt)
                    ss["align_tgt"] = tgt
                except Exception as e:
                    ss.pop("align_check", None)
                    st.error(f"obj_id check failed - {type(e).__name__}: {str(e)[:200]}")
        ac = ss.get("align_check")
        if ac and ss.get("align_tgt") == tgt:
            _v = {"in_place": "✅ in place (updates)", "would_duplicate": "⚠️ WOULD DUPLICATE",
                  "new": "🆕 new (created)"}
            st.dataframe(pd.DataFrame([{"Name": r["name"], "Type": r["type"],
                                        "source obj_id": r["obj_id"], "verdict": _v.get(r["verdict"], r["verdict"]),
                                        "target obj_id": r["target_obj_id"]} for r in ac["rows"]]),
                         hide_index=True, use_container_width=True)
            dups = ac.get("suggest") or {}
            if dups:
                st.warning(f"{len(dups)} object(s) exist in the target under a DIFFERENT obj_id — "
                           "importing now would create duplicates. Align them first:")
                if st.button("Align on SOURCE + re-snapshot"):
                    with st.spinner("Renaming source obj_ids + re-snapshotting…"):
                        res = pipeline.align_source_obj_ids(dups, ss.get("snap_inputs", {}).get("src"))
                        if res["errors"]:
                            st.error("; ".join(f"{o}->{n}: {m}" for o, n, m in res["errors"]))
                        if res["done"]:
                            _resnapshot()
                        ss["align_check"] = pipeline.check_target_alignment(tgt)
                    st.success(f"Changed {len(res['done'])} source obj_id(s) and re-snapshotted.")
                    st.rerun()
            else:
                st.success("No duplicates — every object either updates in place or is created fresh.")

        # ── Proactive target-warehouse schema pre-check (via the TS connection API) ──
        if st.button("🔍 Pre-check target warehouse (schema parity)"):
            with st.spinner("Introspecting the target connection…"):
                try:
                    ss["preflight"] = pipeline.preflight_connection(tgt)
                    ss["preflight_tgt"] = tgt
                except Exception as e:
                    ss.pop("preflight", None)
                    st.error(f"Pre-check failed - {type(e).__name__}: {str(e)[:200]}")
        pf = ss.get("preflight")
        if pf and ss.get("preflight_tgt") == tgt:
            if pf.get("available") is False:
                st.info("🔍 Pre-check unavailable — " + pf.get("reason", ""))
            elif pf["clean"]:
                st.success(f"Target-warehouse parity OK — every release table + column exists in "
                           f"`{pf['connection']}`. Safe to deploy.")
            else:
                st.warning("Target-warehouse gaps found (add them to the warehouse, or drop + deploy):")
                st.table([{"table": f["table"], "db_table": f.get("db_table"),
                           "status": ("ABSENT" if f.get("table_absent") else
                                      "unchecked" if not f.get("checked") else
                                      f"missing {len(f['missing'])}" if f.get("missing") else "ok"),
                           "missing_columns": ", ".join(f.get("missing") or []),
                           "note": f.get("note", "")} for f in pf["findings"]])
                if pf["drop_tokens"]:
                    if st.button("Drop those columns + dependent vizzes and deploy"):
                        with st.spinner("Deploying without the missing columns…"):
                            ss["deploy_result"] = pipeline.deploy(tgt, validate_only=only,
                                                                  drop_cols=pf["drop_tokens"])
                        ss["deploy_tgt"] = tgt
                        st.rerun()

        if st.button(f"{'Validate' if only else 'Deploy'} → {tgt}", type="primary"):
            with st.spinner("Validating + deploying…"):
                ss["deploy_result"] = pipeline.deploy(tgt, validate_only=only)
            ss["deploy_tgt"] = tgt

        r = ss.get("deploy_result")
        if r and ss.get("deploy_tgt") == tgt:
            st.write(f"**Target:** `{r['target']}` (org {r['org']})")
            st.write("**Validate:**")
            st.table([{"status": v["status"], "type": v["type"], "name": v["name"],
                       "error": v.get("error") or ""} for v in r["validate"]])
            if r.get("dropped"):
                st.info(f"Deployed with {len(r['dropped'])} column-drop(s) applied (warehouse-missing).")

            findings = r.get("findings") or []
            if findings:
                st.write("**What went wrong — and how to fix it:**")
                for f in findings:
                    msg = f.get("message") or f.get("kind", "error")
                    fix = f.get("fix")
                    st.error(msg + (f"\n\n➡ {fix}" if fix else ""))
                # offer to drop warehouse-missing columns (+ dependents) and re-deploy
                dropcols = sorted({tok for f in findings if f.get("kind") == "missing_warehouse_column"
                                   for tok in (f.get("drop") or [])})
                if dropcols:
                    st.warning(f"{len(dropcols)} column(s) missing in the target warehouse: "
                               + ", ".join(dropcols))
                    if st.button("Drop those columns + dependent vizzes and re-deploy"):
                        with st.spinner("Re-deploying without the missing columns…"):
                            ss["deploy_result"] = pipeline.deploy(tgt, validate_only=only, drop_cols=dropcols)
                        st.rerun()

            if r.get("blocked"):
                st.error("Validate failed - nothing imported (see above).")
            elif r.get("imported"):
                st.write("**Import:**")
                st.table([{"status": v["status"], "type": v["type"], "name": v["name"],
                           "new_id": v.get("new_id"), "error": v.get("error") or ""} for v in r["imported"]])
                st.success(f"Deployed to `{tgt}`. Re-run is idempotent.")

                # ── Post-import reconciliation: verify against the LIVE target ──
                with st.spinner("Reconciling against the target org…"):
                    try:
                        tc = pipeline.org_client(r["org"], role="target")
                        rec = reconcile(tc, r["imported"])
                    except Exception as e:
                        rec = None
                        st.warning(f"Reconcile skipped - {type(e).__name__}: {str(e)[:160]}")
                if rec is not None:
                    st.write("**Reconciliation (live target truth):**")
                    st.table([{"verdict": f["verdict"], "type": f["type"],
                               "name": f["name"], "detail": f.get("detail", "")} for f in rec])
                    bad = [f for f in rec if f["verdict"] in ("duplicate", "missing")]
                    if bad:
                        st.error(f"{len(bad)} object(s) did not reconcile cleanly: "
                                 + ", ".join(f"{f['name']} ({f['verdict']})" for f in bad))
                    else:
                        st.success("All promoted objects verified present on the target.")

                # ── Optional: promote Spotter coaching (BETA; needs 10.15.0.cl+) ──
                with st.expander("Promote Spotter coaching (optional · beta · needs 10.15.0.cl+)"):
                    st.caption("Carries Spotter NL instructions across orgs (not in TML). Merge = add "
                               "source's to the target's; Replace = target ends with only the source's. "
                               "Not yet verified on a live cluster - use with care.")
                    mode = st.radio("Spotter NL instructions", ["Skip", "Merge", "Replace"],
                                    horizontal=True, key="spotter_mode")
                    if mode != "Skip" and st.button("Promote Spotter coaching"):
                        models = [{"name": v["name"], "obj_id": v.get("new_id"), "source_guid": None}
                                  for v in r["imported"] if v.get("type") == "LOGICAL_TABLE"]
                        try:
                            sc = pipeline.org_client(os.environ.get("TS_ORG_SOURCE", "0"))
                            tc2 = pipeline.org_client(r["org"], role="target")
                            rep = nl_instructions.promote(sc, tc2, models, mode=mode.lower())
                            st.table([{ "model": x.get("name"), "available": x.get("available"),
                                        "result": x.get("reason") or x.get("status") or "ok"} for x in rep])
                        except Exception as e:
                            st.error(f"Spotter promote failed - {type(e).__name__}: {str(e)[:200]}")

# ── repo state ─────────────────────────────────────────────────────────────────────
with tabs[4]:
    st.subheader("Git release + audit trail")
    if st.button("Refresh"):
        st.session_state.pop("io_repo", None)
    if "io_repo" not in st.session_state:
        try:
            g = pipeline.git()
            st.session_state.io_repo = {
                "files": sorted(f for f in g.read_area(pipeline._release_area()) if f.endswith(".tml")),
                "commits": [(c.sha[:8], c.commit.message.splitlines()[0])
                            for c in g._repo.get_commits(sha="main")[:10]],
            }
        except Exception as e:
            st.session_state.io_repo = {"files": [], "commits": [], "error": str(e)[:200]}
    state = st.session_state.io_repo
    if state.get("error"):
        st.warning(f"Could not read the git store: {state['error']} (configure it in Setup).")
    st.markdown("**`release/` (parameterized, org-agnostic)**")
    for f in state["files"]:
        st.write(f"`{f}`")
    if state["commits"]:
        st.markdown("**Commit history (`main`)**")
        st.table([{"sha": s, "message": m} for s, m in state["commits"]])
