"""The generic repository guard (documents, databases, keys) and the CI that runs it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_no_documents.sh"


def _repo(tmp_path: Path, files: list[str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    subprocess.run(["git", "add", "-A", "-f"], cwd=repo, check=True)
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, check=False, timeout=60
    )


@pytest.mark.parametrize(
    "name",
    [
        "contract.pdf",
        "docs/act.docx",
        "bid.xlsx",
        "Bid.XLSX",
        "macro.docm",
        "old.xls",
        "old.doc",
        "backup.zip",
        "x.7z",
        "x.tar.gz",
        "data/app.db",
        "app.db",
        "app.db-wal",
        "x.sqlite3",
        "server.pem",
        "private.key",
        "cert.p12",
        ".env",
        ".env.production",
        "sub/.env",
        "id_rsa",
        "samples/anything.txt",
        "eval_runs/score.json",
        ".eval_cache/parse.json",
        "AI Test/readme.txt",
        "tests/fixtures/app.db",  # a database is never a fixture
        "tests/fixtures/server.key",
        "tests/not_fixtures/contract.docx",
        "src/contract.pdf",
    ],
)
def test_it_refuses(tmp_path: Path, name: str) -> None:
    repo = _repo(tmp_path, ["README.md", name])
    done = _run(repo)
    assert done.returncode == 1, (name, done.stdout, done.stderr)
    assert name.split("/")[-1].lower() in done.stderr.lower() or name.lower() in done.stderr.lower()


@pytest.mark.parametrize(
    "link", ["samples", ".eval_cache", "eval_runs", "data", "AI Test", "models"]
)
def test_it_refuses_a_link_to_a_client_directory(tmp_path: Path, link: str) -> None:
    """A git worktree links the main checkout's client directories in; ``git add -A`` stages the
    link as one path (``samples``), which the ``samples/*`` pattern does not see."""
    repo = _repo(tmp_path, ["README.md"])
    target = tmp_path / "elsewhere"
    target.mkdir()
    (repo / link).symlink_to(target, target_is_directory=True)
    subprocess.run(["git", "add", "-A", "-f"], cwd=repo, check=True)
    done = _run(repo)
    assert done.returncode == 1, (link, done.stdout, done.stderr)
    assert link.lower() in done.stderr.lower()


def test_the_links_are_ignored_by_git_so_that_add_all_does_not_stage_them(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["README.md"])
    (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"))
    target = tmp_path / "elsewhere"
    target.mkdir()
    for link in ("samples", ".eval_cache", "eval_runs", "data", "models"):
        (repo / link).symlink_to(target, target_is_directory=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    staged = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.split()
    assert sorted(staged) == [".gitignore", "README.md"]


@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        "src/app.py",
        ".env.example",
        "tests/fixtures/contract.docx",  # the synthetic fixtures
        "tests/fixtures/compare/base.docx",
        "tests/fixtures/offer.pdf",
        "tests/fixtures/bid.xlsx",
        "docs/diagram.png",
        "uv.lock",
        "scripts/run.sh",
    ],
)
def test_it_accepts(tmp_path: Path, name: str) -> None:
    repo = _repo(tmp_path, ["README.md", name])
    done = _run(repo)
    assert done.returncode == 0, (name, done.stdout, done.stderr)


def test_it_checks_only_the_named_files_when_given_some(tmp_path: Path) -> None:
    """The pre-commit hook passes the staged names; a file elsewhere is not its business."""
    repo = _repo(tmp_path, ["README.md", "old/leftover.pdf"])
    assert _run(repo, "README.md").returncode == 0
    assert _run(repo, "old/leftover.pdf").returncode == 1


def test_every_offender_is_listed_not_just_the_first(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["a.pdf", "b.docx", "c.xlsx", "ok.txt"])
    done = _run(repo)
    assert done.returncode == 1
    for name in ("a.pdf", "b.docx", "c.xlsx"):
        assert name in done.stderr
    assert "ok.txt" not in done.stderr


def test_the_repository_itself_is_clean() -> None:
    done = subprocess.run(
        [str(SCRIPT)], cwd=ROOT, capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr


def test_the_pre_commit_hook_uses_the_same_script() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {h["id"]: h for r in config["repos"] for h in r["hooks"]}
    assert hooks["block-documents"]["entry"] == "scripts/check_no_documents.sh"
    assert hooks["block-documents"]["pass_filenames"] is True


# --- the workflow ----------------------------------------------------------------


WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_ci_runs_the_guard_and_the_offline_proof() -> None:
    jobs = WORKFLOW["jobs"]
    assert {"repo-guard", "offline", "lint", "test", "audit", "secrets"} <= set(jobs)
    guard_steps = " ".join(str(s.get("run", "")) for s in jobs["repo-guard"]["steps"])
    assert "scripts/check_no_documents.sh" in guard_steps
    offline_steps = " ".join(str(s.get("run", "")) for s in jobs["offline"]["steps"])
    assert "scripts/verify_offline.sh" in offline_steps


def test_ci_is_least_privilege() -> None:
    # YAML reads the bare key `on` as the boolean True
    triggers = WORKFLOW.get("on") or WORKFLOW.get(True)
    assert "pull_request_target" not in triggers, (
        "runs untrusted code with the repository's secrets"
    )
    assert WORKFLOW["permissions"] == {"contents": "read"}
    for name, job in WORKFLOW["jobs"].items():
        assert "permissions" not in job or all(v == "read" for v in job["permissions"].values()), (
            name
        )
        assert "secrets." not in str(job), name
