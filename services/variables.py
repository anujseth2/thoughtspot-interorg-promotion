"""ThoughtSpot Variable API helpers (per-org parameterization).

Variables let one parameterized TML resolve to per-org values. You define a variable
once (in the primary org), then assign org-scoped values. Each org's value is applied
at import/runtime in that org.

Notes:
  - Managed by an instance admin from the PRIMARY org.
  - Per-Org secret keys / some variable types may need enabling by ThoughtSpot Support.
  - Variable types: TABLE_MAPPING (db/schema/table), CONNECTION_PROPERTY (account/
    warehouse/role), CONNECTION_PROPERTY_PER_PRINCIPAL, FORMULA_VARIABLE.
"""
from typing import Iterable, List


def create_variable(client, name: str, var_type: str,
                    data_type: str = None, is_sensitive: bool = False) -> dict:
    body = {"name": name, "type": var_type, "is_sensitive": is_sensitive}
    if data_type:
        body["data_type"] = data_type
    return client._post("/api/rest/2.0/template/variables/create", body)


def ensure_variable(client, name: str, var_type: str, **kw) -> bool:
    """Create the variable if it doesn't already exist (idempotent). Returns True if created."""
    try:
        create_variable(client, name, var_type, **kw)
        return True
    except Exception:
        return False  # already exists (or insufficient privilege — caller should verify)


def set_org_value(client, identifier: str, org_identifier: str,
                  values: Iterable[str], operation: str = "REPLACE") -> dict:
    """Assign this variable's value(s) for a specific org."""
    return client._post(
        f"/api/rest/2.0/template/variables/{identifier}/update-values",
        {"variable_assignment": [{"assigned_values": list(values),
                                  "org_identifier": org_identifier}],
         "operation": operation})


def search_variables(client) -> List[dict]:
    try:
        data = client._post("/api/rest/2.0/template/variables/search", {})
        return data if isinstance(data, list) else data.get("variables", data.get("variable", []))
    except Exception:
        return []
