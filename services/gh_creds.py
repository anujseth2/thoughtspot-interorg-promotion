"""Resolve GitHub creds + per-doc TML filenames.

GITHUB_REPO is set in this POC's .env (not a secret). GITHUB_TOKEN is reused from the
sibling ts-git-migration-poc/.env if not set locally, so the token value never has to
be copied into this project.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

import config as C
from services.param_transform import tml_type

ROOT = Path(__file__).resolve().parent.parent
_MIGRATION_ENV = ROOT.parent / "ts-git-migration-poc" / ".env"


def github_repo() -> str:
    repo = os.environ.get("GITHUB_REPO")
    if not repo:
        raise SystemExit("GITHUB_REPO not set in .env (e.g. anujseth2/ts-area-promo)")
    return repo


def github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token and _MIGRATION_ENV.exists():
        load_dotenv(_MIGRATION_ENV)             # pull token from the migration POC
        token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN not found (set it in .env or ts-git-migration-poc/.env)")
    return token


def tml_filename(doc: dict) -> str:
    """Stable, area-independent filename: `<base>.<type>.tml` (e.g. orders.table.tml).

    The base is the obj_id with its area suffix stripped, so the same object lands on
    the same filename in config/, test/ and live/ — promotions overwrite in place and
    `git diff config/ test/` lines the areas up file-for-file.
    """
    typ = tml_type(doc) or "object"
    name = (doc.get(typ) or {}).get("name", "")
    base = C.base_key(doc.get("obj_id", ""), name)
    return f"{base}.{typ}.tml"
