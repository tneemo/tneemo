#!/usr/bin/env python3
"""update-actions.py - SHA-pinned GitHub Actions updater (review-gated).

Reads the canonical list of pinned actions from ``pinned-actions.yaml`` (repo
root), resolves each tracked major tag to its current 40-hex SHA via the
GitHub API (the same call a human reviewer would make), and opens/updates a
pull request that writes ONLY 40-hex SHAs into the action consumers.

Design guarantees (ADR-0001 / spec.md):

* The script can only ever emit a 40-hex SHA. It never writes a tag string
  (``@v4`` / ``@latest`` / ``@main``), so reintroducing a mutable tag is
  structurally impossible.
* The scraper resolves ONLY the tracked major (``v4`` / ``v5``). Crossing a
  major (e.g. ``v4`` -> ``v7``) is excluded from automation - a human edits
  ``pinned-actions.yaml`` for that.
* If the resolver returns anything other than a 40-hex ``[0-9a-f]{40}``, the
  run aborts WITHOUT opening/updating a PR.
* Action-SHA PRs are human-gated: this script NEVER auto-merges them. It only
  auto-merges a PR that touches README content and no action ref.

The GitHub API call used to resolve a tag -> SHA is::

    gh api repos/<owner>/<repo>/git/refs/tags/<tag> --jq '.object.sha'

This is the authoritative tag -> SHA mapping on GitHub.

Testable units
--------------
* ``resolve_sha``        - wraps the ``gh`` call, validates the 40-hex shape.
* ``parse_action_line``  - parses a ``uses: owner/repo@sha  # comment`` line.
* ``rewrite_uses``       - rewrites the SHA on a ``uses:`` line, keeping comment.
* ``propose_changes``    - computes (old, new) per pin and whether anything moved.
* ``is_readme_only``     - decides auto-merge eligibility.

These are pure (or accept an injectable resolver) so they can be unit-tested
without network access.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Callable, Optional

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
PINNED_YAML = os.path.join(REPO_ROOT, "pinned-actions.yaml")

PR_TITLE = "chore(deps): bump pinned GitHub Actions to verified SHAs [skip ci]"
PR_BRANCH = "bot/action-sha-bumps"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Matches:   uses: actions/checkout@<sha>   # comment..........
USES_RE = re.compile(
    r"^(?P<indent>\s*-?\s*)uses:\s*(?P<owner>[^/@]+)/(?P<repo>[^@\s]+)@(?P<ref>\S+)"
    r"(?P<rest>(?:\s+#.*)?)$"
)


def resolve_sha_via_gh(owner: str, repo: str, tag: str) -> str:
    """Resolve ``tag`` to a 40-hex SHA via ``gh api``.

    Raises ``RuntimeError`` if the API call fails or returns a non-SHA value.
    (In tests, swap this with a fake resolver.)
    """
    try:
        out = subprocess.run(
            [
                "gh", "api",
                f"repos/{owner}/{repo}/git/refs/tags/{tag}",
                "--jq", ".object.sha",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"gh api failed for {owner}/{repo} tag {tag}: "
            f"{(e.stderr or '').strip() or 'exit ' + str(e.returncode)}"
        ) from e
    sha = out.stdout.strip()
    if not SHA_RE.match(sha):
        raise RuntimeError(
            f"resolver returned a non-40-hex value for {owner}/{repo}@{tag}: {sha!r}"
        )
    return sha


def parse_action_line(line: str) -> Optional[dict]:
    """Parse a ``uses: owner/repo@ref # comment`` line.

    Returns a dict with keys ``indent``, ``owner``, ``repo``, ``ref``,
    ``comment`` (without the leading ``#``), and ``raw`` (the original line),
    or ``None`` if the line is not a ``uses:`` action reference.
    """
    m = USES_RE.match(line.rstrip("\n"))
    if not m:
        return None
    rest = m.group("rest")
    comment = ""
    if rest:
        comment = rest.strip()[1:].strip()  # strip leading '#'
    return {
        "indent": m.group("indent"),
        "owner": m.group("owner"),
        "repo": m.group("repo"),
        "ref": m.group("ref"),
        "comment": comment,
        "raw": line,
    }


def rewrite_uses(line: str, new_sha: str, comment: str) -> str:
    """Rewrite the SHA on a ``uses:`` line, preserving leading whitespace,
    the owner/repo, and (optionally) the human-readable comment.

    The original comment is preserved when present; if it is empty and a new
    comment is supplied, the new comment is attached. A newline is preserved
    if the input had one.
    """
    parsed = parse_action_line(line)
    if parsed is None:
        return line
    newline = "\n" if line.endswith("\n") else ""
    if comment:
        return (
            f"{parsed['indent']}uses: {parsed['owner']}/{parsed['repo']}@"
            f"{new_sha}  # {comment}{newline}"
        )
    return f"{parsed['indent']}uses: {parsed['owner']}/{parsed['repo']}@{new_sha}{newline}"


def load_pinned(path: str = PINNED_YAML) -> list[dict]:
    """Load the canonical pinned-action list (PyYAML)."""
    import yaml

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "actions" not in data:
        raise ValueError(f"{path}: missing top-level 'actions' key")
    actions = data["actions"]
    if not isinstance(actions, list) or not actions:
        raise ValueError(f"{path}: 'actions' must be a non-empty list")
    return actions


def propose_changes(
    actions: list[dict],
    resolver: Callable[[str, str, str], str],
) -> tuple[list[dict], list[str]]:
    """Resolve each tracked tag and build the proposed change set.

    Returns ``(changes, errors)`` where each change is a dict with keys
    ``owner``, ``repo``, ``tag``, ``old_sha`` (from the workflow today),
    ``new_sha`` (resolved), ``workflow``, ``comment``, and ``changed`` (bool).

    If resolution fails for ANY pin, the errors list is non-empty and NO
    partial change set should be applied (caller aborts).
    """
    changes: list[dict] = []
    errors: list[str] = []
    for a in actions:
        owner = a["owner"]
        repo = a["repo"]
        tag = a["tag"]
        workflow = a["workflow"]
        comment = a.get("step_comment", "")
        try:
            new_sha = resolver(owner, repo, tag)
        except RuntimeError as e:
            errors.append(str(e))
            continue
        # Guardrail (T2.1): never accept a non-40-hex resolution, even if the
        # injected resolver returns one. This is what makes the "abort on a
        # bad SHA" behavior independent of the resolver implementation.
        if not SHA_RE.match(new_sha):
            errors.append(
                f"resolver returned a non-40-hex value for {owner}/{repo}@{tag}: "
                f"{new_sha!r}"
            )
            continue
        old_sha = current_sha_in_workflow(workflow, owner, repo)
        changes.append(
            {
                "owner": owner,
                "repo": repo,
                "tag": tag,
                "old_sha": old_sha,
                "new_sha": new_sha,
                "workflow": workflow,
                "comment": comment,
                "changed": old_sha != new_sha,
            }
        )
    return changes, errors


def current_sha_in_workflow(workflow: str, owner: str, repo: str) -> str:
    """Return the SHA (or tag) currently referenced for owner/repo in the
    given workflow file, or '' if not found."""
    path = os.path.join(REPO_ROOT, workflow)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = parse_action_line(line)
            if p and p["owner"] == owner and p["repo"] == repo:
                return p["ref"]
    return ""


def apply_changes(changes: list[dict]) -> list[str]:
    """Write the proposed SHAs into each workflow file and pinned-actions.yaml.

    Returns the list of file paths that were changed.
    """
    modified: list[str] = []
    # 1. Rewrite each workflow's `uses:` lines.
    workflows_touched: set[str] = set()
    for ch in changes:
        wf = os.path.join(REPO_ROOT, ch["workflow"])
        with open(wf, encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            p = parse_action_line(line)
            if p and p["owner"] == ch["owner"] and p["repo"] == ch["repo"]:
                new_lines.append(rewrite_uses(line, ch["new_sha"], ch["comment"]))
            else:
                new_lines.append(line)
        with open(wf, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        workflows_touched.add(ch["workflow"])
        if wf not in modified:
            modified.append(wf)
    # 2. Update the SHA fields inside pinned-actions.yaml (kept in sync).
    if _sync_pinned_shas(changes):
        if PINNED_YAML not in modified:
            modified.append(PINNED_YAML)
    return modified


def _sync_pinned_shas(changes: list[dict]) -> bool:
    """Update the ``sha:`` field on each action entry in pinned-actions.yaml.

    Returns True if the file was modified.
    """
    by_key = {(c["owner"], c["repo"]): c["new_sha"] for c in changes}
    with open(PINNED_YAML, encoding="utf-8") as f:
        lines = f.readlines()
    out = []
    cur_owner = cur_repo = None
    changed = False
    for line in lines:
        m = re.match(r"^\s*- owner:\s*(\S+)\s*$", line)
        if m:
            cur_owner = m.group(1)
        m = re.match(r"^\s+repo:\s*(\S+)\s*$", line)
        if m:
            cur_repo = m.group(1)
        m = re.match(r"^(\s+)sha:\s*(\S+)\s*$", line)
        if m and (cur_owner, cur_repo) in by_key:
            indent, old = m.group(1), m.group(2)
            new_sha = by_key[(cur_owner, cur_repo)]
            if old != new_sha:
                out.append(f"{indent}sha: {new_sha}\n")
                changed = True
                continue
        out.append(line)
    if changed:
        with open(PINNED_YAML, "w", encoding="utf-8") as f:
            f.writelines(out)
    return changed


def build_pr_body(changes: list[dict]) -> str:
    """Build the PR description, including the resolver output per pin so a
    reviewer can re-verify each SHA by re-running the one-liner."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "Automated SHA-pin bumps for tracked GitHub Actions.",
        "",
        "Resolved via `gh api repos/<owner>/<repo>/git/refs/tags/<tag> --jq '.object.sha'` "
        "(the call a reviewer would make). Only 40-hex SHAs are written - no mutable tags.",
        "",
        "| Action | Tag | Resolved SHA |",
        "|---|---|---|",
    ]
    for c in changes:
        lines.append(f"| {c['owner']}/{c['repo']} | {c['tag']} | `{c['new_sha']}` |")
    lines.append("")
    lines.append(f"_Resolved {today} (UTC). Human review required before merge._")
    return "\n".join(lines)


def run_oss_generator() -> None:
    """Run the existing OSS-section generator (pure content, no action ref)."""
    script = os.path.join(REPO_ROOT, ".github", "scripts", "generate-oss-section.py")
    if not os.path.exists(script):
        return
    env = dict(os.environ)
    # The generator needs a token; prefer GITHUB_TOKEN when present (CI).
    if not env.get("GH_TOKEN") and env.get("GITHUB_TOKEN"):
        env["GH_TOKEN"] = env["GITHUB_TOKEN"]
    subprocess.run([sys.executable, script], check=False, env=env)


def readme_changed() -> bool:
    r = subprocess.run(
        ["git", "diff", "--quiet", "README.md"], cwd=REPO_ROOT
    )
    return r.returncode != 0


def is_readme_only(modified_files: list[str]) -> bool:
    """Auto-merge eligibility: a change set is safe to auto-merge only when it
    touches README content and NO action ref (workflow / pinned-actions.yaml)."""
    if not modified_files:
        return False
    for f in modified_files:
        if f in (PINNED_YAML,):
            return False
        if f.endswith(".github/workflows/update-oss.yml"):
            return False
    return "README.md" in modified_files


# ---- PR plumbing (gh) -------------------------------------------------------

def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], cwd=REPO_ROOT, check=check,
        capture_output=True, text=True,
    )


def pr_exists(branch: str) -> Optional[str]:
    r = _gh("pr", "list", "--head", branch, "--json", "number,url",
            "--jq", ".[0].number", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def open_pr(body: str) -> str:
    r = _gh("pr", "create", "--title", PR_TITLE, "--body", body,
            "--head", PR_BRANCH, "--base", "main")
    m = re.search(r"https?://\S+", r.stdout)
    return m.group(0) if m else r.stdout.strip()


def update_pr(number: str, body: str) -> None:
    _gh("pr", "edit", number, "--body", body)


def commit_and_push(modified: list[str]) -> None:
    _gh("checkout", "-B", PR_BRANCH)
    _gh("add", *modified)
    _gh("commit", "-m", PR_TITLE, check=False)
    _gh("push", "--force", "--set-upstream", "origin", PR_BRANCH)


def auto_merge_pr(number: str) -> None:
    """Only ever called for README-only (no action ref) changes."""
    _gh("pr", "merge", number, "--merge", "--auto", check=False)


# ---- Orchestration ----------------------------------------------------------

def run(dry_run: bool = False, resolver: Optional[Callable] = None) -> int:
    resolver = resolver or resolve_sha_via_gh
    actions = load_pinned()
    changes, errors = propose_changes(actions, resolver)

    # Print resolved state (always - also serves the M2 dry-run gate).
    for c in changes:
        mark = "CHANGED" if c["changed"] else "ok"
        print(f"[{mark}] {c['owner']}/{c['repo']}@{c['tag']} "
              f"-> {c['new_sha']} (was {c['old_sha'] or 'absent'})")
    for e in errors:
        print(f"[ERROR] {e}", file=sys.stderr)

    # Guardrail: a non-SHA resolution aborts the whole run. Never partial.
    if errors:
        print("Aborting: resolver returned a non-40-hex value for at least one "
              "pin. No PR opened.", file=sys.stderr)
        return 2

    if dry_run:
        print("--dry-run: no changes written, no PR opened.")
        return 0

    # Apply action-SHA changes.
    modified = apply_changes(changes)

    # OSS content refresh (relocated here from update-oss.yml).
    run_oss_generator()
    if readme_changed() and "README.md" not in modified:
        modified.append("README.md")

    if not modified:
        print("Nothing to update.")
        return 0

    commit_and_push(modified)
    body = build_pr_body(changes)
    existing = pr_exists(PR_BRANCH)
    if existing:
        update_pr(existing, body)
        print(f"Updated PR #{existing}")
        pr_num = existing
    else:
        url = open_pr(body)
        print(f"Opened PR: {url}")
        pr_num = pr_exists(PR_BRANCH)

    # Auto-merge ONLY when the change set touches no action ref.
    if pr_num and is_readme_only(modified):
        auto_merge_pr(pr_num)
        print(f"README-only PR #{pr_num} auto-merged (no action ref).")
    else:
        print(f"Action-SHA PR #{pr_num} left for human review (no auto-merge).")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="SHA-pinned GitHub Actions updater")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve and print, but write nothing and open no PR.")
    args = p.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
