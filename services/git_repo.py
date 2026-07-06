"""
Git store (GitOps) for the parameterized cross-org release.

Layout on the `main` branch:
    release/    <base>.<type>.tml   (parameterized TML: ${ts_db}/${ts_schema}, obj_id kept)
    variables/  manifest.json + targets.json (the per-org bindings)

One org-agnostic release is stored and versioned in `release/`; each target org binds
its own variable values at deploy time, so the same TML deploys to every org. The
read_area / commit_area helpers operate on a named folder (here, `release/`).

Uses the PyGitHub tree/blob/commit mechanics for a clean single commit per snapshot.
"""

import hashlib
import os
from pathlib import Path
from typing import Dict, Optional

from github import Github, GithubException, InputGitTreeElement


class AreaGitRepo:
    def __init__(self, token: str, repo_name: str, main_branch: str = "main",
                 verify=True, base_url: str = ""):
        # verify: True | False | CA-bundle path (corporate TLS-inspection proxy).
        # base_url: blank for github.com; GitHub Enterprise Server = https://<host>/api/v3.
        kwargs = {"verify": verify}
        if base_url:
            kwargs["base_url"] = base_url.rstrip("/")
        self._gh = Github(token, **kwargs)
        self._repo = self._gh.get_repo(repo_name)
        self.main = main_branch

    # ── read ──────────────────────────────────────────────────────────────────
    def read_area(self, area: str, ref: Optional[str] = None) -> Dict[str, str]:
        """All .tml under `<area>/` on `ref` (default main).

        Returns {relative_path: yaml_string} with the area prefix stripped, so
        keys look like `orders.table.tml`.
        """
        ref = ref or self.main
        files: Dict[str, str] = {}
        try:
            contents = self._repo.get_contents(area, ref=ref)
        except GithubException:
            return files  # folder doesn't exist yet
        queue = list(contents)
        while queue:
            item = queue.pop(0)
            if item.type == "dir":
                queue.extend(self._repo.get_contents(item.path, ref=ref))
            elif item.name.endswith(".tml"):
                rel = item.path[len(area) + 1:]
                files[rel] = item.decoded_content.decode("utf-8")
        return files

    def head_sha(self, ref: Optional[str] = None) -> str:
        return self._repo.get_branch(ref or self.main).commit.sha

    # ── write ─────────────────────────────────────────────────────────────────
    def commit_area(self, area: str, files: Dict[str, str], message: str,
                    branch: Optional[str] = None,
                    reset_from: Optional[str] = None) -> str:
        """Commit {rel_path: yaml} under `<area>/` to `branch` (default main).

        If `branch` does not exist it is created from `reset_from` (or main). If it
        exists and `reset_from` is given, it is force-reset to that base first, so a
        re-run of a promotion produces a clean single-commit branch. Returns the SHA.
        Only the given area's files are touched; other areas on the branch are left
        intact (we build on the existing tree).
        """
        branch = branch or self.main
        base = reset_from or self.main

        try:
            cur = self._repo.get_branch(branch)
            branch_exists = True
        except GithubException:
            branch_exists = False

        # Parent for the new commit. With reset_from, parent is the BASE tip, so the branch
        # is set to "base + these files" in a SINGLE ref update - it never passes through a
        # "0 commits ahead of base" state, which would make GitHub auto-close an open PR.
        # Without reset_from we append to the branch (or seed it from base if it's new).
        if reset_from or not branch_exists:
            parent = self._repo.get_branch(base).commit
        else:
            parent = cur.commit

        blobs = []
        for rel, content in files.items():
            blob = self._repo.create_git_blob(content, "utf-8")
            blobs.append(InputGitTreeElement(
                path=f"{area}/{rel}", mode="100644", type="blob", sha=blob.sha,
            ))

        base_tree = self._repo.get_git_tree(parent.commit.tree.sha)
        new_tree = self._repo.create_git_tree(blobs, base_tree)
        new_commit = self._repo.create_git_commit(message, new_tree, [parent.commit])
        if branch_exists:
            # force only when we reset lineage from base; a plain append is fast-forward
            self._repo.get_git_ref(f"heads/{branch}").edit(new_commit.sha, force=bool(reset_from))
        else:
            self._repo.create_git_ref(f"refs/heads/{branch}", new_commit.sha)
        return new_commit.sha

    # ── PR (the promotion review gate) ──────────────────────────────────────────
    def open_pr(self, head_branch: str, title: str, body: str) -> str:
        head = f"{self._repo.owner.login}:{head_branch}"   # GitHub's head filter needs owner:branch
        for pr in self._repo.get_pulls(state="open", base=self.main, head=head):
            return pr.html_url  # reuse the already-open PR for this branch
        pr = self._repo.create_pull(title=title, body=body,
                                    head=head_branch, base=self.main)
        return pr.html_url

    def merge_pr(self, head_branch: str) -> bool:
        head = f"{self._repo.owner.login}:{head_branch}"
        for pr in self._repo.get_pulls(state="open", base=self.main, head=head):
            pr.merge(merge_method="squash", commit_title=pr.title,
                     commit_message="Merged via area-promotion tool.")
            return True
        return False

    def put_file(self, path: str, content: str, message: str,
                 branch: Optional[str] = None) -> None:
        """Create or update a single file at an arbitrary path (default main)."""
        branch = branch or self.main
        try:
            existing = self._repo.get_contents(path, ref=branch)
            self._repo.update_file(path, message, content, existing.sha, branch=branch)
        except GithubException:
            self._repo.create_file(path, message, content, branch=branch)

    def delete_file(self, path: str, message: str, branch: Optional[str] = None) -> bool:
        """Delete a single file (default main). Returns True if it existed and was removed."""
        branch = branch or self.main
        try:
            cf = self._repo.get_contents(path, ref=branch)
            self._repo.delete_file(path, message, cf.sha, branch=branch)
            return True
        except GithubException:
            return False

    # ── bootstrap ───────────────────────────────────────────────────────────────
    def ensure_area_folders(self, areas) -> None:
        """Create `<area>/.gitkeep` on main for any area folder that doesn't exist yet,
        so read_area never 404s on a fresh repo."""
        for area in areas:
            try:
                self._repo.get_contents(area, ref=self.main)
            except GithubException:
                self._repo.create_file(
                    f"{area}/.gitkeep", f"chore: scaffold {area}/ area folder",
                    "", branch=self.main,
                )


class _NoCommits:
    """Stand-in so the UI repo-state panel (which reads _repo.get_commits) doesn't crash
    in local mode; in local mode the git history lives in your own clone, not here."""
    def get_commits(self, *args, **kwargs):
        return []


class LocalRepo:
    """Filesystem-backed store: read/write the release in ANY local folder, e.g. a path
    inside your own git clone. No GitHub API, token, or branch protection - you manage git
    (add / commit / push / PR) yourself. Mirrors the AreaGitRepo methods the pipeline uses,
    and creates subfolders on write. Selected by setting GIT_LOCAL_DIR in the environment.
    """

    def __init__(self, root: str, main_branch: str = "main"):
        self.root = Path(root).expanduser()
        self.main = main_branch
        self._repo = _NoCommits()

    def read_area(self, area: str, ref: Optional[str] = None) -> Dict[str, str]:
        base = self.root / area
        files: Dict[str, str] = {}
        if base.is_dir():
            for p in sorted(base.rglob("*.tml")):
                rel = str(p.relative_to(base)).replace(os.sep, "/")
                files[rel] = p.read_text(encoding="utf-8")
        return files

    def head_sha(self, ref: Optional[str] = None) -> str:
        return "local"

    def commit_area(self, area: str, files: Dict[str, str], message: Optional[str] = None,
                    branch: Optional[str] = None, reset_from: Optional[str] = None) -> str:
        for rel, content in files.items():
            dest = self.root / area / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        return hashlib.sha1("".join(sorted(files)).encode("utf-8")).hexdigest()

    def put_file(self, path: str, content: str, message: Optional[str] = None,
                 branch: Optional[str] = None) -> None:
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    def delete_file(self, path: str, message: Optional[str] = None,
                    branch: Optional[str] = None) -> bool:
        dest = self.root / path
        if dest.exists():
            dest.unlink()
            return True
        return False

    def ensure_area_folders(self, areas) -> None:
        for area in areas:
            (self.root / area).mkdir(parents=True, exist_ok=True)
