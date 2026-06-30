"""
ThoughtSpot REST API v2 client.
Wraps metadata search, TML export, and TML import.
"""

from datetime import datetime

import requests
from typing import List, Dict, Optional


METADATA_TYPES = ["LOGICAL_TABLE", "LIVEBOARD", "ANSWER"]

# subtype that identifies Models (formerly Worksheets)
MODEL_SUBTYPE = "PRIVATE_WORKSHEET"


class TSClient:
    def __init__(self, host: str, token: str = "",
                 username: str = "", password: str = "", org_id: str = "",
                 secret_key: str = "", verify=True):
        self.host      = host.rstrip("/")
        self._username = username
        self._password = password
        self._secret   = secret_key
        self._org_id   = org_id
        self._session  = requests.Session()
        # verify: True (default), a path to a CA bundle (PEM), or False to skip verification.
        # Set it BEFORE any auth call (token mint) below. False is for a trusted corporate
        # TLS-inspection proxy that re-signs HTTPS with an internal CA Python doesn't know.
        self._session.verify = verify
        if verify is False:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        # Auth precedence: trusted-auth secret key (mint a per-org token, works when
        # SSO/MFA blocks basic login) > username/password session > pre-minted token.
        if secret_key and username:
            self._token_login()
        elif username and password:
            self._session_login()
        elif token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    def _session_login(self):
        """Login via session cookie — correctly scopes to org_id."""
        payload = {
            "username": self._username,
            "password": self._password,
        }
        if self._org_id:
            payload["org_identifier"] = self._org_id
        # Use self._session so cookies are set automatically
        resp = self._session.post(
            f"{self.host}/api/rest/2.0/auth/session/login",
            json=payload,
            timeout=180,
        )
        resp.raise_for_status()

    def _token_login(self):
        """Mint a full-access token via the trusted-auth secret_key and use it as a
        bearer token, scoped to org_id. Works when basic login is blocked by SSO/MFA.
        Short-lived; _reauth re-mints on a 401."""
        payload = {"username": self._username, "secret_key": self._secret,
                   "validity_time_in_sec": 3600}
        if self._org_id:
            payload["org_id"] = int(self._org_id)
        resp = self._session.post(
            f"{self.host}/api/rest/2.0/auth/token/full", json=payload, timeout=180)
        resp.raise_for_status()
        self._session.headers["Authorization"] = "Bearer " + resp.json()["token"]

    def _reauth(self):
        """Re-establish auth using whichever method this client was created with."""
        if self._secret and self._username:
            self._token_login()
        elif self._username and self._password:
            self._session_login()

    def refresh_token(self):
        """Re-establish auth (session or minted token) if it expires."""
        self._reauth()

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(f"{self.host}{path}", json=payload, timeout=180)
        if resp.status_code == 401 and self._username and (self._password or self._secret):
            # Session/token expired - re-auth and retry once
            self._reauth()
            resp = self._session.post(f"{self.host}{path}", json=payload, timeout=180)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.text.strip():
            return {}  # tags/assign, share, update-obj-id return 204 No Content
        return resp.json()

    # ── Metadata search ───────────────────────────────────────────────────────

    def search_metadata(self, prefix: str) -> List[Dict]:
        """
        Return all Models, Liveboards, and Answers whose name starts with prefix.
        """
        import json as _json
        import os as _os
        results = []
        _debug_dumped = False

        for obj_type in METADATA_TYPES:
            offset = 0
            while True:
                payload = {
                    "metadata": [{"type": obj_type}],
                    "record_size": 200,
                    "record_offset": offset,
                }
                data = self._post("/api/rest/2.0/metadata/search", payload)

                # API returns a list directly
                items = data if isinstance(data, list) else data.get("metadata", [])

                # One-shot debug: dump first matching item so we can see exact keys
                if not _debug_dumped and items:
                    try:
                        with open("/tmp/ts_search_sample.json", "w") as _f:
                            _json.dump(items[0], _f, indent=2, default=str)
                        _debug_dumped = True
                    except Exception:
                        pass

                for item in items:
                    name = item.get("metadata_name", "")
                    if name.lower().startswith(prefix.lower()) and name.lower().endswith("_br"):
                        header = item.get("metadata_header", {}) or {}
                        mod_ms = item.get("metadata_modified_time") or header.get("modified")
                        try:
                            modified = (
                                datetime.fromtimestamp(mod_ms / 1000).strftime("%Y-%m-%d %H:%M")
                                if mod_ms else ""
                            )
                        except (TypeError, ValueError, OSError):
                            modified = ""
                        author = (
                            header.get("authorDisplayName")
                            or header.get("authorName")
                            or header.get("author_display_name")
                            or header.get("author_name")
                            or ""
                        )
                        results.append({
                            "id":       item.get("metadata_id", ""),
                            "name":     name,
                            "type":     obj_type,
                            "modified": modified,
                            "author":   author,
                        })

                if len(items) < 200:
                    break
                offset += 200

        return results

    # ── TML export ────────────────────────────────────────────────────────────

    def export_tml(self, object_ids: List[str]) -> dict:
        """
        Export TML for given object IDs with obj_id and FQN included.
        Returns the raw API response dict (SpotApp.from_api expects this).
        """
        payload = {
            "metadata": [{"identifier": oid} for oid in object_ids],
            "export_options": {
                "include_obj_id":     True,
                "include_obj_id_ref": True,
            },
        }
        return self._post("/api/rest/2.0/metadata/tml/export", payload)

    # ── TML import ────────────────────────────────────────────────────────────

    def import_tml(self, tml_strings: List[str],
                   policy: str = "PARTIAL") -> List[Dict]:
        """
        Import a list of TML YAML strings.
        policy: PARTIAL | ALL_OR_NONE | VALIDATE_ONLY
        Returns per-object result list.
        """
        payload = {
            "metadata_tmls": tml_strings,
            "import_policy": policy,
        }
        resp = self._session.post(
            f"{self.host}/api/rest/2.0/metadata/tml/import",
            json=payload,
            timeout=300,
        )
        if resp.status_code == 401 and self._username and (self._password or self._secret):
            self._reauth()
            resp = self._session.post(
                f"{self.host}/api/rest/2.0/metadata/tml/import",
                json=payload,
                timeout=300,
            )
        data = resp.json()

        # Normalise — API may return list or {"object": [...]}
        if isinstance(data, list):
            raw = data
        else:
            raw = data.get("object", [])

        results = []
        for item in raw:
            # API returns {"response": {"header": {...}, "status": {...}}, "request_index": N}
            response = item.get("response", item)  # fallback to item itself for older format
            header   = response.get("header", {})
            status   = response.get("status", {})
            status_code = status.get("status_code", "UNKNOWN")
            error_msg   = status.get("error_message", "")
            # "Existing guid ... will be used" is an informational update notice, not a failure
            if status_code != "OK" and "will be used" in error_msg:
                status_code = "OK"
                error_msg   = ""
            results.append({
                "name":    header.get("name", "unknown"),
                "type":    header.get("metadata_type", ""),
                "status":  status_code,
                "error":   error_msg,
                "new_id":  header.get("id_guid", header.get("owner_guid", "")),
            })
        return results

    # ── Tags ──────────────────────────────────────────────────────────────────
    def ensure_tag(self, tag_name: str, color: str = "") -> None:
        """Create the tag if it does not already exist (idempotent)."""
        try:
            body = {"name": tag_name}
            if color:
                body["color"] = color
            self._post("/api/rest/2.0/tags/create", body)
        except Exception:
            pass  # already exists

    def assign_tag(self, metadata: List[Dict], tag_name: str) -> dict:
        """metadata: [{'type': API_TYPE, 'identifier': obj_id_or_guid}, ...]"""
        return self._post("/api/rest/2.0/tags/assign",
                          {"metadata": metadata, "tag_identifiers": [tag_name]})

    # ── Sharing ───────────────────────────────────────────────────────────────
    def share_with_group(self, metadata: List[Dict], group: str,
                         mode: str = "READ_ONLY", message: str = "area share") -> dict:
        # NOTE: metadata identifiers must be GUIDs (obj_id is NOT accepted here),
        # and `message` is required by the API.
        return self._post("/api/rest/2.0/security/metadata/share", {
            "metadata": metadata,
            "permissions": [{"principal": {"type": "USER_GROUP", "identifier": group},
                             "share_mode": mode}],
            "message": message,
        })

    # ── obj_id ────────────────────────────────────────────────────────────────
    def set_obj_id(self, current_obj_id: str, new_obj_id: str) -> dict:
        """Rename an object's obj_id (e.g. clean up an auto-generated one)."""
        return self._post("/api/rest/2.0/metadata/update-obj-id",
                          {"metadata": [{"current_obj_id": current_obj_id,
                                         "new_obj_id": new_obj_id}]})

    # ── List objects by type (tag-free source selection) ───────────────────────
    def list_objects(self, types: List[str]) -> List[Dict]:
        out: List[Dict] = []
        for typ in types:
            data = self._post("/api/rest/2.0/metadata/search",
                              {"metadata": [{"type": typ}], "record_size": -1})
            items = data if isinstance(data, list) else data.get("metadata", [])
            for it in items:
                hdr = it.get("metadata_header", {}) or {}
                tags = [(t.get("name", "") if isinstance(t, dict) else str(t))
                        for t in (hdr.get("tags") or [])]
                out.append({"id": it.get("metadata_id", ""),
                            "name": it.get("metadata_name", ""),
                            "obj_id": it.get("metadata_obj_id", ""),
                            "type": it.get("metadata_type", ""),
                            "tags": [t for t in tags if t]})
        return out

    # ── Search by area tag ──────────────────────────────────────────────────────
    def search_by_tag(self, tag: str, types: List[str]) -> List[Dict]:
        """Find objects carrying a tag (e.g. 'area:config'), restricted to types."""
        out: List[Dict] = []
        for typ in types:
            data = self._post("/api/rest/2.0/metadata/search",
                              {"metadata": [{"type": typ}], "tag_identifiers": [tag],
                               "record_size": -1})
            items = data if isinstance(data, list) else data.get("metadata", [])
            for it in items:
                out.append({"id": it.get("metadata_id", ""),
                            "name": it.get("metadata_name", ""),
                            "obj_id": it.get("metadata_obj_id", ""),
                            "type": it.get("metadata_type", "")})
        return out

    # ── Export object + its dependency chain ────────────────────────────────────
    def export_associated_edocs(self, object_ids: List[str]) -> List[str]:
        """Export TML for the given objects AND their dependencies (model, tables).
        Returns a list of edoc strings. NOTE: confirm the response shape against a
        real export from the sandbox org, then tighten the parsing below."""
        data = self._post("/api/rest/2.0/metadata/tml/export", {
            "metadata": [{"identifier": oid} for oid in object_ids],
            "export_associated": True,
            "export_options": {"include_obj_id": True, "include_obj_id_ref": True},
        })
        items = data if isinstance(data, list) else data.get("object", data.get("metadata", []))
        return [it.get("edoc", "") for it in items if it.get("edoc")]

    # ── Connection schema introspection (live, from the CDW) ────────────────────
    def connection_columns(self, conn_identifier: str, database: str,
                           schema: str, table: str) -> Dict[str, str]:
        """Physical columns of <database>.<schema>.<table> in a connection's warehouse.
        Returns {column_name: data_type}. Needs DATAMANAGEMENT privilege; hits the CDW.
        """
        data = self._post("/api/rest/2.0/connection/search", {
            "connections": [{"identifier": conn_identifier,
                             "data_warehouse_objects": [{"database": database,
                                                         "schema": schema, "table": table}]}],
            "data_warehouse_object_type": "COLUMN",
            "record_size": -1, "record_offset": 0,
        })
        conns = data if isinstance(data, list) else data.get("connections", data.get("connection", []))
        cols: Dict[str, str] = {}
        for c in conns:
            dwo = c.get("data_warehouse_objects") or {}
            for db in dwo.get("databases", []) or []:
                for sch in db.get("schemas", []) or []:
                    for tbl in sch.get("tables", []) or []:
                        for col in tbl.get("columns", []) or []:
                            if col.get("name"):
                                cols[col["name"]] = col.get("data_type")
        return cols


# TML type -> REST metadata type enum (for share / tags/assign)
_API_TYPE = {
    "liveboard": "LIVEBOARD", "answer": "ANSWER",
    "model": "LOGICAL_TABLE", "worksheet": "LOGICAL_TABLE",
    "table": "LOGICAL_TABLE", "view": "LOGICAL_TABLE", "sql_view": "LOGICAL_TABLE",
    "connection": "CONNECTION",
}


def api_metadata_type(tml_type: str) -> str:
    return _API_TYPE.get(tml_type, "LOGICAL_TABLE")
