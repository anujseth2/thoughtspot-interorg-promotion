# ThoughtSpot Inter-Org Promotion (variable-based)

Promote ThoughtSpot content **across orgs** (e.g. a Primary authoring org → Dev/QA orgs
→ Production tenant orgs) by publishing **one parameterized set of TML** and letting
each org supply its own values through ThoughtSpot **Variables**.

This is the cross-org companion to the intra-org area-promotion tool. The difference is
how per-environment differences are handled: instead of rewriting connection/database
names for each target, the TML carries `${variable}` tokens and each org is assigned its
own values.

## Why variables
ThoughtSpot's rule for TML that is portable across orgs is two things:
1. **obj_id** present — the stable object identity across orgs (we keep it; `guid` is
   stripped because it is cluster-unique).
2. Per-org-varying fields **parameterized with `${...}`** — values are assigned per org
   via the Variable API and resolved at import/runtime.

So one `release/` of TML deploys everywhere; org A and org B just hold different values.

```yaml
table:
  name: Orders
  db: ${ts_db}        # TABLE_MAPPING variable
  schema: ${ts_schema}
  db_table: ORDERS
  connection: { name: Snowflake }
obj_id: orders
```

## Flow
| Verb | What it does |
|------|--------------|
| **snapshot** | export the authoring org's objects (or the seed) → parameterize (db/schema → `${...}`, keep obj_id) → commit `release/` + a variable manifest |
| **setup_vars** | create the TABLE_MAPPING variables in the **Primary** org and assign each target org its values |
| **deploy** | import `release/` into a target org; that org's values resolve the tokens |

```bash
python scripts/snapshot.py --source-org <DEV org id> --tag <release tag>   # build release/ from an org
#   or: python scripts/snapshot.py --from-seed                # build release/ from the bundled sample
python scripts/setup_vars.py                                 # create vars + per-org values (reads variables/targets.json)
python scripts/deploy.py --target dev --validate-only        # validate against a target first
python scripts/deploy.py --target dev                        # deploy to the Dev target
python scripts/deploy.py --target ryans_specialty            # deploy to a Production tenant target
#   targets (dev, ryans_specialty, ...) are keys in variables/targets.json
```

## Setup
1. Python 3.9+: `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
2. `cp .env.example .env` and fill it in (TS creds, `TS_ORG_PRIMARY`, `GITHUB_REPO`, `GITHUB_TOKEN`).
3. `cp variables/targets.example.json variables/targets.json` and add one entry per target org: a friendly name, its `org_id`, the `connection` name in that org, and `values` (`ts_db` / `ts_schema`).

## Prerequisites / dependencies
- **Variables managed from the Primary org by an admin.** Creating variables in a tenant
  org returns 403; do it in Primary (`TS_ORG_PRIMARY=0`).
- The **Variables feature** must be enabled on the cluster (some variants need ThoughtSpot
  Support to switch on).
- For true environment **isolation**, pair this with **per-org secret keys** (each
  environment's token service holds only its own org's secret) and **per-org CORS** — see
  the Access Provisioning & Admin Runbook.
- Connections are assumed to exist per org (same name/obj_id). Connection properties can
  also be parameterized via `CONNECTION_PROPERTY` variables if you promote connections.

## Layout
```
config.py                  variable names + parameterization rules
services/
  param_transform.py       parameterize TML (db/schema -> ${...}, keep obj_id, strip guid)
  variables.py             Variable API helpers (create, per-org values, search)
  pipeline.py              the three verbs
  ts_client.py             ThoughtSpot REST v2 client
  git_repo.py              Git layer (release/ + manifest)
  gh_creds.py              GITHUB_REPO / GITHUB_TOKEN
scripts/                   snapshot.py, setup_vars.py, deploy.py
seed/                      sample Orders content
variables/targets.example.json  per-target template (name, org_id, connection, values)
tests/test_param.py        offline transform test (no org needed)
```

Offline sanity check: `python tests/test_param.py`.
