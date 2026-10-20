# ThoughtSpot Inter-Org Promotion

Promote ThoughtSpot content (Liveboards, Answers, Models, and the Tables they depend on)
from one org to another. Instead of hand-editing a copy for each org, you publish **one
parameterized release** and each target org binds it to its own data. The same release
deploys to every org and produces the same content, pointed at that org's database.

## How it works

- **Stable identity, disposable cluster id.** Each object keeps its `obj_id` (its
  cross-org identity, matched on import) and has its `guid` (cluster-specific) stripped.
  The first deploy creates the object and later deploys update it in place instead of
  duplicating it.
- **Parameterized data binding.** The only per-org differences (database and schema) are
  replaced with `${ts_db}` / `${ts_schema}`. Each target org supplies its own values, and
  the connection is pointed at that org's connection by name on deploy.
- **One release, many targets.** A snapshot is taken from a source org and carries no
  target. The target is chosen at deploy time, so one snapshot can be deployed to any
  number of orgs, one deploy per target.
- **Additive release.** A snapshot adds or updates the files it produces and leaves every
  other file in the release untouched, so promoting one module never removes another.
- **You deploy what you select.** The Deploy tab lists the release's assets with their
  status in the target and deploys only the ones you select.
- **Safe by construction.** Every deploy validates first and imports only if every object
  validates, as a single all-or-nothing import. It never deletes. Re-running is idempotent.

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

1. **Primary connection.** Enter the host and choose an auth method (see
   [Credentials](#credentials)). Click **Test connection & load orgs**. With a secret key
   or a login, Setup connects through the Primary org to list the orgs on the cluster; if
   your user isn't a member of the Primary org, the page asks for an org it does belong to
   and connects through that. Bearer tokens each stay in the org they were generated in.
2. **Orgs in this promotion.** Pick the orgs involved and set each one's **role**
   (`source` / `target`). For targets, set the **connection** and the **database/schema**
   it reads from. **Load connections** lists the target org's own connections and **Fetch
   dbs** reads its databases, then the schemas of the one you pick, straight from the
   connection, so all three become dropdowns. If a connection returns nothing (a cold
   warehouse, a missing permission, or a per-user OAuth connection), type them in instead.
   A saved value that isn't in a loaded list stays selected and is flagged, never swapped.
3. **Git store.** Choose **Local folder** or **GitHub repo** (the release store). In GitHub
   mode you can also set the pull request template (see
   [Pull request description](#pull-request-description)). Then **Save configuration**.

Setup writes `.env` (host, credential and settings) and `variables/orgs.json` (roles and
bindings). Both are git-ignored.

## The promotion flow

### 1. Snapshot

Choose the source org and what to promote: **Pick assets**, **By tag**, **By collection**,
or **All objects in the org**. When you pick assets, their dependencies (models and tables)
are pulled in automatically, so you only pick the top-level Liveboards, Answers or Models.

Before taking the snapshot, open **obj_id alignment vs target** on the selection view and
click **Check obj_id alignment** to see how each object will land in a target: as new,
as an update to the same obj_id already there, or as a duplicate because the target holds
an object of that name under a different obj_id. **Align these on the SOURCE org**
rewrites the source objects' obj_ids to match the target's, so they update in place.

The snapshot exports the TML, parameterizes it, and writes it to `release/` in the release
store, together with `variables/last_promotion.json`, the list of files that snapshot
produced.

- **GitHub mode** commits to the release branch (`GIT_BRANCH`) and opens a pull request
  into the base branch (`GIT_BASE_BRANCH`, default `main`), so it works with a protected
  base branch. Write the description for this promotion in the **Pull request
  description** box on the Snapshot tab before you run it.
- **Local folder mode** writes the files to the folder. You commit, push and open the pull
  request yourself.

### 2. Review and merge the pull request

The pull request is the review gate and the record of who promoted what, and when. Merge
it before taking the next snapshot (see [Known limitations](#known-limitations)).

Deploy works before or after the merge. It reads the release from the release branch while
the pull request is open, and from the base branch once it has been merged and the branch
deleted.

### 3. Variables

Create the `ts_db` / `ts_schema` variables and assign each target org's values, or turn on
**Resolve variables locally** in Setup and skip this step. See
[How each org's values are applied](#how-each-orgs-values-are-applied).

### 4. Deploy

Pick a target and click **Check against target**. Each asset in the release is listed with
its status in that org:

- **new**: not in the target yet, so it will be created.
- **in target**: present under the same obj_id, so it will be updated in place.
- **would DUPLICATE**: present under the same name but a different obj_id. Align it first
  (step 1) or it will be created as a second object.

The assets from the latest snapshot are pre-selected. Use the search box, the type and
status filters, and **Select all shown** / **Deselect all** to adjust the selection.
Dependencies of a selected asset that are in the release but not yet in the target are
included automatically.

**Validate selection** runs a validate-only import. **Deploy selection** validates again and
imports only if every object passes, as one all-or-nothing import, then reports each object
as **created new** or **updated in place**. If the target warehouse is missing columns the
release uses, the tool names them and offers **Drop those columns + dependent vizzes and
re-deploy**.

After the import, **Reconciliation** re-queries the live target and reports each object as
**created**, **updated** (in place), **duplicate** (its name now resolves to more than one
object) or **missing** (the import reported OK but nothing is there).

Optionally, **Promote Spotter coaching** carries the deployed models' Spotter NL
instructions across, either merged with the target's or replacing them (beta, needs
10.15.0.cl or later). NL instructions are not part of TML, so a deploy alone does not move
them.

### Repo state

Lists the release files in the store and, in GitHub mode, the recent commits on `main`.

## Pull request description

GitHub applies a repository's `pull_request_template.md` only to pull requests opened in
its web UI, not to ones created through the API, so the tool fills in the description
itself. The box on the Snapshot tab is pre-filled from:

1. the template saved on the Setup page (`variables/pr_template.md`, git-ignored), or if
   that is empty,
2. the release repo's own `pull_request_template.md` on the base branch.

Whatever is in the box when you snapshot is submitted as the description. **Load from
repo** on the Setup page copies the repo's template into the Setup box so you can adapt it.
If there is no template anywhere and the box is left empty, the pull request gets a
one-line default. When a snapshot reuses a pull request that is still open, its
description is updated to the latest one.

## Credentials

The Setup page offers three auth methods:

- **Secret key (trusted auth).** One credential for every org: the tool mints a
  short-lived token for the configured user in each org as it works, so nobody signs in
  interactively. That user must be a member of every org it promotes into. A cluster
  administrator generates the secret when enabling trusted authentication.
- **Bearer token.** A bearer token is bound to the org it was minted for, so you provide two:
  one for the source org (snapshot) and one for the target org (deploy). On connect, the
  Setup page shows which org each token belongs to and flags an org whose token is elsewhere,
  and the tool refuses to snapshot from or deploy to an org with a token from a different one.
- **Username + password.**

Objects the tool creates are authored by the user it authenticates as. To keep promoted
content from showing a person's name as its author, authenticate as a dedicated service
account.

**Where credentials are stored.** Saving the Setup page writes the values on it to `.env`
in plain text beside the application. `.env` is git-ignored but sits on disk. Values that
are already set in the environment when the tool starts take precedence over `.env`, so a
secret store can inject them at launch instead. Note that saving the Setup page still
writes whatever its fields hold to `.env`, and the fields are pre-filled from the
environment, so an injected value is written out if the page is saved.

## How each org's values are applied

- **ThoughtSpot Variables** (managed in the Primary org, org 0, by an admin, scoped per
  org). The platform resolves the tokens. Requires the Variables feature enabled on the
  cluster.
- **Resolved at deploy time** (when Variables are not enabled): the tool substitutes each
  target org's values into the release as it deploys. Same result; the only difference is
  where the substitution happens. Toggle this with **Resolve variables locally** in Setup.

## Known limitations

- A snapshot resets the release branch from the base branch, so if two snapshots are taken
  before the first pull request is merged, only the second remains in the pull request.
  Merge each promotion's pull request before taking the next snapshot.
- Release files are named by obj_id and snapshots never remove files. After an object's
  obj_id is aligned on the source, the next snapshot writes it under its new file name and
  the file for the old obj_id stays in the release, so the Deploy list shows the object
  twice.
- The tool does not create connections, warehouses or tables. Each target's connection must
  already exist, and the tables the content references must be present in its database and
  schema.

## Command line (optional)

The same steps are available as scripts:

```bash
python scripts/snapshot.py    --source-org <source org id> --tag <tag>   # or --collection <guid>, or --from-seed
python scripts/setup_vars.py                                             # create + assign variables
python scripts/deploy.py      --target <org key> --validate-only         # validate first
python scripts/deploy.py      --target <org key>                         # then import
```

From the command line, `deploy.py` deploys the whole release folder rather than a selection.

## Prerequisites

- A credential that reaches the source and target orgs: a trusted-auth secret (simplest, one
  credential for every org), a bearer token per side, or a username and password.
- The **connection** in each target org already exists; the tool points content at it by
  name.
- The **tables** referenced by the content are present in each target org's database and
  schema.
- For server-side variable resolution, the **Variables feature** must be enabled on the
  cluster; otherwise use **Resolve variables locally**.
- For GitHub mode, a token that can create branches and open pull requests on the release
  repo (`repo` scope, or fine-grained Contents and Pull requests write).

## Layout

```
app.py                     Streamlit UI (Setup, Snapshot, Variables, Deploy, Repo state)
config.py                  variable names + parameterization rules
services/
  param_transform.py       parameterize TML (db/schema -> ${...}, keep obj_id, strip guid)
  pipeline.py              snapshot / alignment / setup_vars / deploy
  reconcile.py             post-import check against the live target
  import_diagnostics.py    turn import errors into actionable findings; column drop
  variables.py             Variable API helpers
  feedback_replace.py      Spotter feedback merge/replace helpers (beta, not in the UI)
  nl_instructions.py       Spotter NL instructions promotion (beta)
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
