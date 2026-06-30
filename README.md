# ThoughtSpot Inter-Org Promotion

Promote ThoughtSpot content (Liveboards, Answers, Models, and the Tables they depend on)
from one org to another. Instead of hand-editing a copy for each org, you publish **one
parameterized release** and each target org binds it to its own data. The same release
deploys to every org and produces the same content, pointed at that org's database.

## How it works

- **Stable identity, disposable cluster id.** Each object keeps its `obj_id` (its
  cross-org identity, matched on import) and has its `guid` (cluster-specific) stripped.
  So the first deploy creates the object and a later deploy of the same release updates it
  in place instead of duplicating. Promotion is repeatable.
- **Parameterized data binding.** The only per-org differences (database and schema) are
  replaced with `${ts_db}` / `${ts_schema}`. Each target org supplies its own values; the
  connection is pointed at that org's connection by name on deploy.
- **One credential reaches every org.** Connect once with a trusted-auth secret for an
  admin of the Primary org; the tool mints a short-lived token per org as it works. No
  per-org credentials.
- **Safe by construction.** Every deploy validates first and imports only if validation
  passes. It never deletes. Re-running is idempotent.

## Two repositories, two jobs

| Repo | Holds | Notes |
|------|-------|-------|
| **This repo (source)** | the tool's code | what you clone and run |
| **Release store** (separate) | the parameterized `release/` snapshots | a different GitHub repo *or* a local folder you configure |

The release store is **not** this repo. Point it at a separate repo (`GITHUB_REPO`) or a
local folder (`GIT_LOCAL_DIR`). Never point it at this source repo, or snapshots will be
committed into the code.

## Quick start (web UI)

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Then, in the **Setup** tab:

1. **Step 1 - Primary connection.** Enter the host and a trusted-auth secret for an admin
   of the Primary org. Click **Test connection & load orgs**.
2. **Step 2 - Orgs in this promotion.** Pick the orgs involved and set each one's **role**
   (`source` / `variables` / `target`); for targets, set the **connection** and the
   **database/schema** it reads from.
3. **Git store.** Choose **Local folder** or a **GitHub repo** (the release store), and
   **Save configuration**.

Then run the flow across the tabs: **Snapshot** (pick the assets to promote) ->
**Variables** (or resolve locally) -> **Deploy** (validate, then import).

Setup is fully UI-driven; it writes `.env` (host + the one credential + settings) and
`variables/orgs.json` (roles + bindings). Both are git-ignored and never committed.

## How each org's values are applied

- **ThoughtSpot Variables** (managed in the Primary org by an admin, scoped per org). The
  platform resolves the tokens. Requires the Variables feature enabled on the cluster.
- **Resolved at deploy time** (when Variables are not enabled): the tool substitutes each
  target org's values into the release as it deploys. Same result; the only difference is
  where the substitution happens. Toggle this with **Resolve variables locally** in Setup.

## Command line (optional)

The same three steps are available as scripts:

```bash
python scripts/snapshot.py    --source-org <source org id> --tag <tag>   # or --from-seed
python scripts/setup_vars.py                                             # create + assign variables
python scripts/deploy.py      --target <org key> --validate-only         # validate first
python scripts/deploy.py      --target <org key>                         # then import
```

## Prerequisites

- An **admin trusted-auth secret** for the Primary org (a bare bearer token is org-bound
  and only reaches one org).
- The **connection** in each target org already exists; the tool points content at it by
  name and does not create warehouses or connections.
- The **tables** referenced by the content are present in each target org's database and
  schema.
- For server-side variable resolution, the **Variables feature** must be enabled on the
  cluster; otherwise use **Resolve variables locally**.

## Layout

```
app.py                     Streamlit UI (Setup, Snapshot, Variables, Deploy, Repo state)
config.py                  variable names + parameterization rules
services/
  param_transform.py       parameterize TML (db/schema -> ${...}, keep obj_id, strip guid)
  variables.py             Variable API helpers
  pipeline.py              snapshot / setup_vars / deploy
  ts_client.py             ThoughtSpot REST v2 client
  git_repo.py              release store (local folder or GitHub repo + PR)
  ui_setup.py              UI-driven config (writes .env + variables/orgs.json)
  gh_creds.py              GitHub repo / token
scripts/                   snapshot.py, setup_vars.py, deploy.py, git_bootstrap.py
seed/                      sample content (offline demo)
variables/targets.example.json   example per-org config
tests/test_param.py        offline transform test (no org needed)
```

Offline sanity check: `python tests/test_param.py`.
