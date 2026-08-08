"""Tests for .github/scripts/update-actions.py (unit, no network, no PR).

These exercise the design's guardrails from spec.md / tasks.md:
  T2.1 resolver rejects non-40-hex
  T2.2 writer emits ONLY 40-hex SHAs, preserves comment, zero @v lines
  T2.3 major-bump guard: tracked major only, never crosses to latest major
  T2.4 --dry-run self-check exits 0
  T4.2 README-only change auto-merges; action-SHA change does NOT
"""

import importlib.util
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, ".github", "scripts", "update-actions.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("update_actions", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ua = _load_module()


SAMPLE_WORKFLOW = """name: demo
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      # v4.4.0; pin reviewed 2026-08-04. Update only via a reviewed Dependabot PR.
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
      # v5.6.0; pin reviewed 2026-08-04. Update only via a reviewed Dependabot PR.
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: "3.12"
"""


# ---- T2.1: resolver validation ---------------------------------------------

def test_resolver_rejects_non_40hex():
    def fake_bad(owner, repo, tag):
        return "11d5960aSHORT"  # 12 hex chars, not 40
    actions = [{"owner": "actions", "repo": "checkout", "tag": "v4",
                "workflow": "w.yml", "step_comment": "x"}]
    changes, errors = ua.propose_changes(actions, fake_bad)
    assert errors, "expected a resolution error for non-40-hex"
    assert changes == []  # nothing to apply on a bad resolution


def test_resolver_accepts_40hex():
    good = "11d5960a326750d5838078e36cf38b85af677262"

    def fake_good(owner, repo, tag):
        return good
    actions = [{"owner": "actions", "repo": "checkout", "tag": "v4",
                "workflow": "w.yml", "step_comment": "x"}]
    changes, errors = ua.propose_changes(actions, fake_good)
    assert errors == []
    assert changes[0]["new_sha"] == good


# ---- T2.2: SHA-only writer, comment preserved, zero @v --------------------

def test_rewrite_uses_emits_only_sha_and_keeps_comment(tmp_path):
    wf = tmp_path / "w.yml"
    wf.write_text(SAMPLE_WORKFLOW, encoding="utf-8")
    # Patch the workflow path used by apply_changes via REPO_ROOT-independent helper.
    line = "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262\n"
    new_line = ua.rewrite_uses(line, "a" * 40, "v4.4.0; pin reviewed 2026-08-04")
    assert new_line == (
        "      - uses: actions/checkout@" + "a" * 40 +
        "  # v4.4.0; pin reviewed 2026-08-04\n"
    )
    assert "@v" not in new_line


def test_apply_changes_writes_sha_only_and_preserves_comment(tmp_path, monkeypatch):
    wf = tmp_path / "update-oss.yml"
    wf.write_text(SAMPLE_WORKFLOW, encoding="utf-8")
    monkeypatch.setattr(ua, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(ua, "PINNED_YAML", str(tmp_path / "pinned-actions.yaml"))
    # seed a pinned-actions.yaml so _sync_pinned_shas has something to read
    (tmp_path / "pinned-actions.yaml").write_text(
        "actions:\n"
        "  - owner: actions\n    repo: checkout\n    tag: v4\n    sha: old\n"
        "    workflow: update-oss.yml\n    step_comment: v4.4.0; pin reviewed 2026-08-04\n"
        "  - owner: actions\n    repo: setup-python\n    tag: v5\n    sha: old\n"
        "    workflow: update-oss.yml\n    step_comment: v5.6.0; pin reviewed 2026-08-04\n",
        encoding="utf-8",
    )
    new = "b" * 40
    changes = [
        {"owner": "actions", "repo": "checkout", "tag": "v4",
         "old_sha": "11d5960a326750d5838078e36cf38b85af677262",
         "new_sha": new, "workflow": "update-oss.yml",
         "comment": "v4.4.0; pin reviewed 2026-08-04", "changed": True},
        {"owner": "actions", "repo": "setup-python", "tag": "v5",
         "old_sha": "a26af69be951a213d495a4c3e4e4022e16d87065",
         "new_sha": "c" * 40, "workflow": "update-oss.yml",
         "comment": "v5.6.0; pin reviewed 2026-08-04", "changed": True},
    ]
    modified = ua.apply_changes(changes)
    assert str(wf) in modified
    out = wf.read_text(encoding="utf-8")
    assert "uses: actions/checkout@" + "b" * 40 in out
    assert "uses: actions/setup-python@" + "c" * 40 in out
    # zero mutable tags
    assert not any(l.startswith("      - uses:") and "@v" in l for l in out.splitlines())
    # comments preserved
    assert "pin reviewed" in out
    # pinned-actions.yaml sha synced
    pinned = (tmp_path / "pinned-actions.yaml").read_text(encoding="utf-8")
    assert "sha: " + "b" * 40 in pinned
    assert "sha: " + "c" * 40 in pinned


# ---- T2.3: major-bump guard ------------------------------------------------

def test_major_bump_guard_stays_on_tracked_major():
    # Even though "latest" is v7.0.1, the scraper must resolve the TRACKED
    # major (v4) and must NOT propose v7.
    def fake_resolver(owner, repo, tag):
        # The resolver is told exactly which tag to resolve; it does not read
        # releases/latest. So a v4 tag resolves to whatever v4 points at.
        assert tag == "v4", "scraper must NOT cross to a different major"
        return "11d5960a326750d5838078e36cf38b85af677262"

    actions = [{"owner": "actions", "repo": "checkout", "tag": "v4",
                "workflow": "w.yml", "step_comment": "x"}]
    changes, errors = ua.propose_changes(actions, fake_resolver)
    assert errors == []
    assert changes[0]["tag"] == "v4"
    assert changes[0]["new_sha"] == "11d5960a326750d5838078e36cf38b85af677262"


# ---- T2.4: --dry-run -------------------------------------------------------

def test_dry_run_exits_zero_and_writes_nothing(tmp_path, monkeypatch, capsys):
    wf = tmp_path / "update-oss.yml"
    wf.write_text(SAMPLE_WORKFLOW, encoding="utf-8")
    monkeypatch.setattr(ua, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(ua, "PINNED_YAML", str(tmp_path / "pinned-actions.yaml"))
    (tmp_path / "pinned-actions.yaml").write_text(
        "actions:\n"
        "  - owner: actions\n    repo: checkout\n    tag: v4\n    sha: old\n"
        "    workflow: update-oss.yml\n    step_comment: x\n"
        "  - owner: actions\n    repo: setup-python\n    tag: v5\n    sha: old\n"
        "    workflow: update-oss.yml\n    step_comment: y\n",
        encoding="utf-8",
    )

    def fake_resolver(owner, repo, tag):
        return "d" * 40 if repo == "checkout" else "e" * 40

    rc = ua.run(dry_run=True, resolver=fake_resolver)
    assert rc == 0
    out = capsys.readouterr().out
    assert "--dry-run" in out
    assert "d" * 40 in out and "e" * 40 in out
    # nothing written
    after = wf.read_text(encoding="utf-8")
    assert "d" * 40 not in after


# ---- T4.2: auto-merge decision --------------------------------------------

def test_is_readme_only_allows_readme_only():
    assert ua.is_readme_only(["README.md"]) is True
    assert ua.is_readme_only(["README.md", "other.txt"]) is True


def test_is_readme_only_blocks_action_ref():
    assert ua.is_readme_only([".github/workflows/update-oss.yml"]) is False
    assert ua.is_readme_only(["pinned-actions.yaml"]) is False
    # mixed: an action ref taints the whole change set
    assert ua.is_readme_only(["README.md", ".github/workflows/update-oss.yml"]) is False
    assert ua.is_readme_only([]) is False


# ---- parse helper ----------------------------------------------------------

def test_parse_action_line():
    p = ua.parse_action_line(
        "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4.4.0\n"
    )
    assert p["owner"] == "actions"
    assert p["repo"] == "checkout"
    assert p["ref"] == "11d5960a326750d5838078e36cf38b85af677262"
    assert "v4.4.0" in p["comment"]
    assert ua.parse_action_line("      - name: build\n") is None
