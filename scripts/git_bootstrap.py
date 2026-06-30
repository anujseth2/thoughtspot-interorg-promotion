"""Create the release/ folder for the inter-org promotion store.

Usage:  python scripts/git_bootstrap.py
Modes:
  - GIT_LOCAL_DIR set  -> just creates the release/ subfolder in that local folder
                          (no GitHub; you manage git yourself).
  - otherwise          -> creates the GitHub repo (if missing) + the release/ folder.
                          Needs GITHUB_REPO (owner/name) + GITHUB_TOKEN in .env.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from services.pipeline import RELEASE, git

# Local-folder mode: no GitHub needed, just make the subfolders on disk.
if os.environ.get("GIT_LOCAL_DIR"):
    git().ensure_area_folders([RELEASE])
    print(f"local release folder ready: {os.path.join(os.environ['GIT_LOCAL_DIR'], RELEASE)}")
    raise SystemExit(0)

from github import Github, GithubException

from services.gh_creds import github_repo, github_token
from services.git_repo import AreaGitRepo

repo_name = github_repo()
gh = Github(github_token())
try:
    repo = gh.get_repo(repo_name)
    print(f"repo exists: {repo.full_name}")
except GithubException:
    owner, _, short = repo_name.partition("/")
    me = gh.get_user().login
    print(f"creating {repo_name} (auth user: {me}) …")
    repo = (gh.get_user().create_repo(short, private=True, auto_init=True)
            if owner.lower() == me.lower()
            else gh.get_organization(owner).create_repo(short, private=True, auto_init=True))
    print(f"created: {repo.full_name}")

AreaGitRepo(github_token(), repo_name).ensure_area_folders([RELEASE])
print(f"{RELEASE}/ ready\n\n{repo.html_url}")
