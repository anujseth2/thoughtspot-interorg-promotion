"""UI-driven setup helpers: build a client from explicit config, discover orgs and
connections (so you pick from real values instead of guessing ids/db/schema), and persist
everything to .env + variables/orgs.json. Lets the whole tool be configured in the UI
with no hand-editing of files - which is where setup pain usually lives.

orgs.json holds the per-org info in one place: each org's role (source /
target) and - for targets - the connection and database/schema. The env file holds the
cluster host + the one primary credential, which is used for every org.
"""
import json
import os
from pathlib import Path

from services.ts_client import (TSClient, bearer_only, bearer_token_org, ts_error_code,
                                PRIMARY_ORG, NOT_ORG_MEMBER)

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ORGS_PATH = ROOT / "variables" / "orgs.json"
TARGETS_PATH = ROOT / "variables" / "targets.json"   # legacy, migrated on first load

# env keys written/read (the cluster host + the DEFAULT credential + non-org settings);
# per-org credentials live in orgs.json, not here.
ENV_KEYS = ["TS_HOST", "TS_USER", "TS_PASSWORD", "TS_TOKEN", "TS_TOKEN_TARGET", "TS_SECRET_KEY",
            "TS_CA_BUNDLE", "TS_VERIFY_SSL",
            "TS_CONNECT_ORG", "TS_ORG_SOURCE", "TS_RELEASE_TAG", "TS_RESOLVE_LOCAL",
            "GIT_LOCAL_DIR", "GITHUB_REPO", "GITHUB_TOKEN", "GIT_BRANCH", "GIT_BASE_BRANCH",
            "GITHUB_API_URL", "GIT_BASE_PATH"]


def _verify_from_cfg(cfg: dict):
    ca = (cfg.get("ca_bundle") or "").strip()
    if ca:
        return ca
    return False if cfg.get("verify_ssl", True) is False else True


def _is_bearer(cfg: dict) -> bool:
    return bearer_only(cfg.get("user", ""), cfg.get("password", ""),
                       cfg.get("token", ""), cfg.get("secret", ""))


def bearer_token_orgs(cfg: dict):
    """[(label, org_id, org_name)] for each bearer token in `cfg` (source, then target).
    Empty unless the credential is bearer tokens."""
    if not _is_bearer(cfg):
        return []
    out = []
    for label, tok in (("source", cfg.get("token", "")), ("target", cfg.get("target_token", ""))):
        if tok:
            oid, name = bearer_token_org(cfg["host"], tok, _verify_from_cfg(cfg))
            out.append((label, oid, name))
    return out


def _bearer_for_org(cfg: dict, org_id) -> str:
    """The bearer token in `cfg` that is bound to `org_id`. Raises, naming each token's org,
    when neither is: a token cannot be pointed at another org."""
    found = bearer_token_orgs(cfg)
    for label, oid, _ in found:
        if oid == str(org_id):
            return cfg.get("token" if label == "source" else "target_token", "")
    seen = "; ".join(f"the {label} token belongs to {name} ({oid})" for label, oid, name in found)
    raise RuntimeError(f"No bearer token here is bound to org {org_id} ({seen}). Bearer tokens "
                       "only reach the org they were generated in, so generate one while signed "
                       "in to that org, or use a trusted-auth secret key.")


def make_client(cfg: dict, org_id="", match_org: bool = False) -> TSClient:
    """A client for `org_id`, built from the primary credential in `cfg`. A secret key or a
    username + password reaches every org (a token is minted, or a session opened, per org_id).
    Bearer tokens are bound to the org they were generated in and ignore org_id, so for a
    lookup inside a specific org (match_org=True) use whichever of the source / target tokens
    belongs to that org, and refuse if neither does."""
    token = cfg.get("token", "")
    if match_org and _is_bearer(cfg):
        token = _bearer_for_org(cfg, org_id)
    return TSClient(host=cfg["host"], org_id=str(org_id),
                    username=cfg.get("user", ""), password=cfg.get("password", ""),
                    token=token, secret_key=cfg.get("secret", ""),
                    verify=_verify_from_cfg(cfg))


class NotInConnectOrg(RuntimeError):
    """The credential's user is not a member of the org Setup connects through (the Primary
    org unless one was entered), so ThoughtSpot refused the token. The UI then asks for an org
    the user does belong to."""


def _connect_org(cfg: dict) -> str:
    return str(cfg.get("connect_org") or PRIMARY_ORG).strip() or PRIMARY_ORG


def _connect_client(cfg: dict) -> TSClient:
    """A client for the org Setup connects through. Raises NotInConnectOrg when the user isn't a
    member of it; any other failure (wrong secret, unknown user, network) propagates as is."""
    org = _connect_org(cfg)
    try:
        return make_client(cfg, org)
    except Exception as e:
        code = ts_error_code(e)
        status = getattr(getattr(e, "response", None), "status_code", None)
        where = "the Primary org" if org == PRIMARY_ORG else f"org {org}"
        if code == NOT_ORG_MEMBER:
            raise NotInConnectOrg(
                f"This user isn't a member of {where}, so ThoughtSpot refused a token for it. "
                "Enter the id of an org the user does belong to (the source org works) and "
                "connect again.") from e
        if status == 401 and cfg.get("password") and not cfg.get("secret"):
            # session login answers 401 both for a wrong password and for a non-member org
            raise NotInConnectOrg(
                f"Login to {where} failed. Check the username and password; if they are right, "
                f"the user may not be a member of {where}, so enter an org it does belong to "
                "and connect again.") from e
        raise


def list_orgs(cfg: dict):
    """[(id, name)] for every org, read through the connect org."""
    c = _connect_client(cfg)
    data = c._post("/api/rest/2.0/orgs/search", {})
    orgs = data if isinstance(data, list) else data.get("orgs", [])
    return [(str(o.get("id")), o.get("name")) for o in orgs]


def connect(cfg: dict):
    """Validate the credential and best-effort list the orgs. Returns (orgs, note).

    The credential is validated by the token mint in make_client (raises if it's truly bad).
    orgs/search requires ORG_ADMINISTRATION, so a non-admin gets a 403 there even though they
    can still operate in the orgs they belong to - in that case we return ([], note) so the UI
    can fall back to manual org-ID entry. Promotion does NOT need org-admin."""
    c = _connect_client(cfg)                             # token mint = credential check
    # 1) admin path: orgs/search lists every org on the cluster
    try:
        data = c._post("/api/rest/2.0/orgs/search", {})
        orgs = data if isinstance(data, list) else data.get("orgs", [])
        if orgs:
            return [(str(o.get("id")), o.get("name")) for o in orgs], ""
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 401:
            raise                                        # genuinely bad credential
    # 2) non-admin fallback: auth/session/user (any user can call it) returns the orgs the
    #    user BELONGS to, with id + name - so a non-admin still gets their target org by name.
    me = c._get("/api/rest/2.0/auth/session/user")
    myorgs = me.get("orgs") or ([me["current_org"]] if me.get("current_org") else [])
    orgs = [(str(o.get("id")), o.get("name")) for o in myorgs if o.get("id") is not None]
    return orgs, ("" if orgs else "couldn't list any orgs for this credential - check it has "
                  "access to the source/target orgs")


def list_connections(cfg: dict, org_id):
    """[(id, name)] of connections in an org (pick by name, no typos)."""
    c = make_client(cfg, org_id, match_org=True)
    data = c._post("/api/rest/2.0/metadata/search",
                   {"metadata": [{"type": "CONNECTION"}], "record_size": -1})
    items = data if isinstance(data, list) else data.get("metadata", [])
    return [(it.get("metadata_id"), it.get("metadata_name")) for it in items]


# connection/search returns no warehouse objects unless it is told the connection's own auth type
# (a Databricks PAT connection answers SERVICE_ACCOUNT with nothing). Read it off the connection's
# stored configuration, and fall back across the valid types if that guess comes back empty.
_AUTH_FALLBACKS = ("SERVICE_ACCOUNT", "PERSONAL_ACCESS_TOKEN", "OAUTH_WITH_SERVICE_PRINCIPAL",
                   "OAUTH_WITH_PKCE")


def _connection_auth(c: TSClient, conn_identifier) -> str:
    """The connection's own auth type, inferred from its stored configuration keys, or ""."""
    try:
        rows = c._post("/api/rest/2.0/connection/search",
                       {"connections": [{"identifier": conn_identifier}], "include_details": True,
                        "record_size": -1})
    except Exception:
        return ""
    rows = rows if isinstance(rows, list) else rows.get("connection", [])
    conf = ((rows[0].get("details") or {}).get("configuration") if rows else None) or {}
    if isinstance(conf, str):
        try:
            conf = json.loads(conf)
        except ValueError:
            conf = {}
    keys = set(conf)
    if "personal_access_token" in keys:
        return "PERSONAL_ACCESS_TOKEN"
    if keys & {"oauth_client_id", "oauth_client_secret"}:
        return "OAUTH_WITH_SERVICE_PRINCIPAL"
    if {"user", "password"} <= keys:
        return "SERVICE_ACCOUNT"
    return ""


def list_warehouse_objects(cfg: dict, org_id, conn_identifier, kind: str, database: str = "",
                           auth_hint: str = ""):
    """Names of the DATABASEs on a connection, or the SCHEMAs in one of its databases, read
    through ThoughtSpot (it uses the connection's stored credential; no warehouse secret here).

    Returns (names, auth_type_that_worked). ([], "") when nothing comes back, which happens for a
    cold warehouse, a missing permission, or a per-user OAuth connection; the UI then falls back
    to typing. Pass auth_hint (the type that worked for the databases) to skip re-probing."""
    c = make_client(cfg, org_id, match_org=True)
    first = auth_hint or _connection_auth(c, conn_identifier)
    tries = [a for a in (first,) + _AUTH_FALLBACKS if a]
    tries = list(dict.fromkeys(tries))
    for auth in tries:
        body = {"connections": [{"identifier": conn_identifier}], "data_warehouse_object_type": kind,
                "authentication_type": auth, "record_size": -1}
        if kind == "SCHEMA":
            body["connections"][0]["data_warehouse_objects"] = [{"database": database}]
        try:
            rows = c._post("/api/rest/2.0/connection/search", body)
        except Exception:
            continue                                    # this auth type isn't valid here; try the next
        rows = rows if isinstance(rows, list) else rows.get("connections", rows.get("connection", []))
        dbs = [d for r in rows for d in ((r.get("data_warehouse_objects") or {}).get("databases") or [])]
        if kind == "DATABASE":
            names = [d.get("name") for d in dbs]
        else:
            names = [sc.get("name") for d in dbs if d.get("name") in ("", None, database)
                     for sc in (d.get("schemas") or [])]
        names = sorted({n for n in names if n})
        if names:
            return names, auth
    return [], ""


def load_orgs_config() -> dict:
    """Per-org config for the UI to prefill. Reads orgs.json; if it's absent but a legacy
    targets.json exists, migrates that into the new shape (role=['target'], default auth)
    so nothing is lost."""
    if ORGS_PATH.exists():
        try:
            return json.loads(ORGS_PATH.read_text())
        except Exception:
            return {}
    if TARGETS_PATH.exists():
        try:
            raw = json.loads(TARGETS_PATH.read_text())
        except Exception:
            return {}
        out = {}
        for k, v in raw.items():
            if k.startswith("_"):
                continue
            oid = str(v.get("org_id", ""))
            if oid:
                out[oid] = {"name": v.get("name", oid), "role": ["target"],
                            "connection": v.get("connection", ""),
                            "values": v.get("values", {})}
        return out
    return {}


def env_values(cfg: dict) -> dict:
    return {
        "TS_HOST": cfg.get("host", ""),
        "TS_USER": cfg.get("user", ""),
        "TS_PASSWORD": cfg.get("password", ""),
        "TS_TOKEN": cfg.get("token", ""),
        "TS_TOKEN_TARGET": cfg.get("target_token", ""),
        "TS_SECRET_KEY": cfg.get("secret", ""),
        "TS_CA_BUNDLE": cfg.get("ca_bundle", ""),
        "TS_VERIFY_SSL": "" if cfg.get("verify_ssl", True) else "false",
        "TS_CONNECT_ORG": "" if _connect_org(cfg) == PRIMARY_ORG else _connect_org(cfg),
        "TS_ORG_SOURCE": str(cfg.get("source_org", "")),
        "TS_RELEASE_TAG": cfg.get("tag", ""),
        "TS_RESOLVE_LOCAL": "1" if cfg.get("resolve_local") else "",
        "GIT_LOCAL_DIR": cfg.get("git_local_dir", ""),
        "GITHUB_REPO": cfg.get("github_repo", ""),
        "GITHUB_TOKEN": cfg.get("github_token", ""),
        "GIT_BRANCH": cfg.get("git_branch", ""),
        "GIT_BASE_BRANCH": cfg.get("git_base_branch", ""),
        "GITHUB_API_URL": cfg.get("github_api_url", ""),
        "GIT_BASE_PATH": cfg.get("git_base_path", ""),
    }


def write_config(cfg: dict, orgs_config: dict):
    """Apply config to the running process (os.environ, picked up by the pipeline on the
    next call) and persist it to .env + variables/orgs.json. The source org is derived from
    the roles in orgs_config. Returns the two paths."""
    source = next((oid for oid, r in orgs_config.items()
                   if "source" in (r.get("role") or [])), str(cfg.get("source_org", "")))
    env = env_values(cfg)
    env["TS_ORG_SOURCE"] = str(source)
    # Live for this session, matching what a fresh launch from the saved .env would see: a key
    # with no value is UNSET, not set to "". An empty string would block every
    # os.environ.get(k, default) and any fallback that only fills unset keys, for the rest of
    # the session. Unsetting still clears a credential the new config no longer uses.
    for k in ENV_KEYS:
        if env.get(k):
            os.environ[k] = env[k]
        else:
            os.environ.pop(k, None)
    lines = [f"{k}={env[k]}" for k in ENV_KEYS if env.get(k)]  # only persist the set ones
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ORGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ORGS_PATH.write_text(json.dumps(orgs_config, indent=2), encoding="utf-8")
    return str(ENV_PATH), str(ORGS_PATH)

