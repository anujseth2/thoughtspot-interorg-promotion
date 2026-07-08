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
    auth = st.radio("Primary auth method",
                    ["Secret key (trusted auth)", "Bearer token", "Username + password"],
                    horizontal=True,
                    help="Secret key: SSO/MFA orgs, where basic login is blocked; mints a "
                         "short-lived per-org token.  Bearer token: you already hold one "
                         "(it is bound to the org it was minted for).  Username + password: "
                         "local (non-SSO) accounts only.")
    user = secret = token = password = ""
    if auth.startswith("Secret"):
        user = st.text_input("Username (token is minted for this user)", value=os.environ.get("TS_USER", ""))
        secret = st.text_input("Trusted-auth secret key", value=os.environ.get("TS_SECRET_KEY", ""), type="password")
    elif auth.startswith("Bearer"):
        token = st.text_input("Bearer token", value=os.environ.get("TS_TOKEN", ""), type="password")
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
                st.warning(f"Connected, but {note}. Add the org IDs manually below - promotion "
                           "only needs membership + data rights in the orgs you use, not org-admin.")
            else:
                st.success(f"Connected. Loaded {len(orgs)} orgs. Step 2 is unlocked below.")
        except Exception as e:
            ss.pop("connected", None); ss.pop("orgs", None)
            st.error(f"Connection failed - {type(e).__name__}: {str(e)[:300]}")

    if ss.get("connected"):
        ss.setdefault("orgs", [])
        ss.setdefault("orgs_cfg", {})

        with st.expander("Orgs  ·  add by ID if the list is empty (non-admins can't auto-list orgs)",
                         expanded=not ss["orgs"]):
            m1, m2, m3 = st.columns([2, 3, 1])
            _mid = m1.text_input("Org ID", key="man_org_id")
            _mname = m2.text_input("Name (optional)", key="man_org_name")
            if m3.button("Add", key="man_org_add") and _mid.strip():
                if _mid.strip() not in [i for i, _ in ss["orgs"]]:
                    ss["orgs"].append((_mid.strip(), _mname.strip() or _mid.strip()))
                    st.rerun()
            if ss["orgs"]:
                st.caption("Orgs available: " + ", ".join(f"{n} ({i})" for i, n in ss["orgs"]))

        orgs = ss["orgs"]
        id2name = {i: n for i, n in orgs}
        ids = [i for i, _ in orgs]

        # ── Git store ──
        st.markdown("**Git store**")
        gitmode = st.radio("Where to store the release", ["Local folder", "GitHub repo"], horizontal=True)
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
            try:
                p1, p2 = ui_setup.write_config(_cfg(), new_cfg)
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
    src, tag, object_ids, scope = "", None, None, None
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
            "What to promote", ["Pick assets", "By tag", "All objects in the org"],
            horizontal=True,
            help="Pick assets: choose specific objects; their dependencies (model, tables) "
                 "are pulled in automatically, so you only pick the top-level Liveboard/Answer/Model.")
        if scope == "Pick assets":
            if st.button("List assets in the source org"):
                try:
                    ss["snap_assets"] = pipeline.list_source_assets(src or None)
                    ss.pop("asset_pick", None)
                except Exception as e:
                    ss.pop("snap_assets", None)
                    st.error(f"Couldn't list assets - {type(e).__name__}: {str(e)[:200]}")
            assets = ss.get("snap_assets", [])
            if assets:
                _ty = {"LIVEBOARD": "Liveboard", "ANSWER": "Answer", "LOGICAL_TABLE": "Table/Model"}
                f1, f2 = st.columns([3, 2])
                with f1:
                    q = st.text_input("Search by name or tag", key="asset_q",
                                      placeholder="filter the grid…")
                with f2:
                    types_present = sorted({a["type"] for a in assets})
                    tf = st.multiselect("Filter by type", types_present, default=types_present,
                                        format_func=lambda t: _ty.get(t, t), key="asset_tf")
                ql = (q or "").lower()
                rows = []
                for a in assets:
                    if tf and a["type"] not in tf:
                        continue
                    tags = ", ".join(a.get("tags", []) or [])
                    if ql and ql not in a.get("name", "").lower() and ql not in tags.lower():
                        continue
                    rows.append({"Name": a.get("name", ""), "Type": _ty.get(a["type"], a["type"]),
                                 "Tags": tags, "id": a["id"]})
                df = pd.DataFrame(rows, columns=["Name", "Type", "Tags", "id"])
                sel = st.dataframe(df, hide_index=True, use_container_width=True,
                                   on_select="rerun", selection_mode="multi-row",
                                   column_config={"id": None})  # click headers to sort
                sel_ids = [rows[i]["id"] for i in sel.selection.rows] if rows else []
                a1, a2, a3 = st.columns([2, 2, 3])
                with a1:
                    if st.button(f"Add {len(sel_ids)} to set", disabled=not sel_ids):
                        s = set(ss.get("asset_pick", []))
                        s.update(sel_ids)
                        ss["asset_pick"] = list(s)
                with a2:
                    if st.button("Clear set", disabled=not ss.get("asset_pick")):
                        ss["asset_pick"] = []
                object_ids = ss.get("asset_pick", [])
                with a3:
                    st.caption(f"Promotion set: **{len(object_ids)}** asset(s)  ·  showing {len(rows)} of {len(assets)}")
                if object_ids:
                    idmap = {a["id"]: a for a in assets}
                    st.table([{"Name": idmap.get(i, {}).get("name", i),
                               "Type": _ty.get(idmap.get(i, {}).get("type", ""), "")} for i in object_ids])
            else:
                st.info("Click **List assets in the source org** to choose objects.")
        elif scope == "By tag":
            tag = st.text_input("Tag", value=os.environ.get("TS_RELEASE_TAG", ""))

    if st.button("Snapshot", type="primary"):
        if scope == "Pick assets" and not object_ids:
            st.warning("Add at least one asset to the set (or switch to By tag / All objects).")
        else:
            with st.spinner("Parameterizing + writing release…"):
                r = pipeline.snapshot(source_org=src or None, tag=tag or None,
                                      from_seed=from_seed, object_ids=object_ids or None)
            st.success(f"Wrote {len(r['files'])} file(s) to `release/` @ `{r['sha'][:8]}`")
            st.write("variables referenced:", r["variables"])
            if r.get("source_bindings"):
                st.info("Source tables were bound to (use these as the target **ts_db / ts_schema** "
                        "unless the target differs):  "
                        + ";   ".join(f"`{b['db']} / {b['schema']}`" for b in r["source_bindings"]))
            st.table([{"file": f} for f in r["files"]])
            if r["warnings"]:
                st.warning(r["warnings"])

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
            with st.spinner("Setting up variables…"):
                r = pipeline.setup_vars(values_by_org)
            st.success(f"created {r['created'] or '(all existed)'}; {len(r['assigned'])} value(s) assigned")
            st.table(r["assigned"])

# ── 3 · deploy ─────────────────────────────────────────────────────────────────────
with tabs[3]:
    st.subheader("Deploy release to a target org")
    st.write("Reads `release/`, remaps the connection to the target org's, imports "
             "(tables first). VALIDATE_ONLY runs first and blocks the import if it fails. Never deletes.")
    targets = pipeline._targets()
    if not targets:
        st.warning("No targets configured - add them in the Setup tab.")
    else:
        tgt = st.selectbox("Target", list(targets.keys()),
                           format_func=lambda k: f"{targets[k].get('name', k)}  ({k})")
        only = st.checkbox("Validate only (no import)", value=True)
        if st.button(f"{'Validate' if only else 'Deploy'} → {tgt}", type="primary"):
            with st.spinner("Validating + deploying…"):
                r = pipeline.deploy(tgt, validate_only=only)
            st.write(f"**Target:** `{r['target']}` (org {r['org']})")
            st.write("**Validate:**")
            st.table([{"status": v["status"], "type": v["type"], "name": v["name"],
                       "error": v.get("error") or ""} for v in r["validate"]])
            if r.get("blocked"):
                st.error("Validate failed - nothing imported.")
            elif r.get("imported"):
                st.write("**Import:**")
                st.table([{"status": v["status"], "type": v["type"], "name": v["name"],
                           "new_id": v.get("new_id"), "error": v.get("error") or ""} for v in r["imported"]])
                st.success(f"Deployed to `{tgt}`. Re-run is idempotent.")

# ── repo state ─────────────────────────────────────────────────────────────────────
with tabs[4]:
    st.subheader("Git release + audit trail")
    if st.button("Refresh"):
        st.session_state.pop("io_repo", None)
    if "io_repo" not in st.session_state:
        try:
            g = pipeline.git()
            st.session_state.io_repo = {
                "files": sorted(f for f in g.read_area(pipeline.RELEASE) if f.endswith(".tml")),
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
