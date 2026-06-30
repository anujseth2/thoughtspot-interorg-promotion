"""Inter-org promotion config: variable names + parameterization rules.

The same parameterized TML deploys to every org; each org supplies its own values for
these variables via the Variable API (scoped by org). Variable names are the tokens
that appear in the TML as ${name}.
"""
import os

# TABLE_MAPPING variables — per-org database / schema binding on Table/View objects.
DB_VAR = os.environ.get("TS_DB_VAR", "ts_db")
SCHEMA_VAR = os.environ.get("TS_SCHEMA_VAR", "ts_schema")

# CONNECTION_PROPERTY variables — per-org connection binding (optional; connections are
# often pre-created per org instead of promoted).
ACCOUNT_VAR = os.environ.get("TS_ACCOUNT_VAR", "ts_account")
WAREHOUSE_VAR = os.environ.get("TS_WAREHOUSE_VAR", "ts_warehouse")
ROLE_VAR = os.environ.get("TS_ROLE_VAR", "ts_role")

# What each variable maps to, for the create-variables step.
TABLE_MAPPING_VARS = {DB_VAR: "database", SCHEMA_VAR: "schema"}
CONNECTION_PROPERTY_VARS = {ACCOUNT_VAR: "accountName", WAREHOUSE_VAR: "warehouse", ROLE_VAR: "role"}


def ref(var: str) -> str:
    """The TML token for a variable, e.g. ts_db -> ${ts_db}."""
    return "${" + var + "}"
