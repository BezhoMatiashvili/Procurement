"""The offline release bundle (scripts/make_release_bundle.sh), run against a temporary copy of the
repository: HEAD from `git archive` plus the working-tree changes, committed in a fresh git
repository. `docker` is a stub placed first on PATH; the image archive is a tiny fake tarball."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/make_release_bundle.sh"
EPOCH = "1700000000"  # 2023-11-14T22:13:20Z

#: The files the bundle may hold, from the repository, and the ones it writes itself. A change to
#: the script's allow-list has to change this list too: that is the point of repeating it here.
ALLOWED_FROM_REPO = [
    ".env.example",
    "README.md",
    "docker-compose.yml",
    "docs/PRODUCTION_READINESS.md",
    "docs/SECURITY.md",
    "docs/THIRD_PARTY.md",
    "docs/sbom.cdx.json",
]
GENERATED = ["INSTALL.md", "RELEASE.txt", "SHA256SUMS"]

#: Tracked files at paths that must never reach a bundle, each holding its own canary.
POISON = [
    "samples/secret.pdf",
    "data/app.db",
    ".env",
    "eval_runs/run.json",
    ".eval_cache/parse.json",
    ".claude/settings.json",
    "AI Test/contract.docx",
    "tests/leak.py",
    "scripts/leak.sh",
    "src/leak.py",
    "models/docling/weights.bin",
    "docs/ACCURACY.md.bak",
    "build/old/leak.txt",
]
DENIED_PARTS = {
    "samples",
    "data",
    ".env",
    "eval_runs",
    ".eval_cache",
    ".claude",
    "AI Test",
    "tests",
    "scripts",
    "src",
    "models",
    "build",
    "dist",
    ".git",
}

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
GIT += ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]

STUB_DOCKER = """#!/bin/sh
# a stand-in for docker: `image inspect --format F TAG` and `save TAG...`
cmd="$1"; shift
case "$cmd" in
  image)
    [ "$1" = inspect ] || exit 2
    shift
    fmt=""; tag=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --format) fmt="$2"; shift 2 ;;
        *) tag="$1"; shift ;;
      esac
    done
    case "$tag" in
      procurement-agent:*|ollama/ollama:*) ;;
      *) echo "Error: No such image: $tag" >&2; exit 1 ;;
    esac
    case "$fmt" in
      *RepoDigests*) case "$tag" in ollama/*) printf 'ollama/ollama@sha256:%064d ' 5 ;; esac ;;
      *.Id*) printf 'sha256:%064d\\n' 7 ;;
      *.Size*) echo 12345 ;;
    esac
    ;;
  save)
    cat "$FAKE_IMAGE_TAR"
    ;;
  *) exit 2 ;;
esac
"""


def _run(
    args: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=120
    )


def _git(repo: Path, *args: str) -> str:
    done = _run([*GIT, *args], repo)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.fixture(scope="module")
def base_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """HEAD of this repository plus the working tree's changes, as a new one-commit repository,
    with tracked files at the paths that must never be bundled."""
    repo = tmp_path_factory.mktemp("bundle-base") / "repo"
    repo.mkdir()
    archive = subprocess.run(
        ["git", "archive", "HEAD"], cwd=ROOT, capture_output=True, check=True, timeout=60
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(repo)], input=archive, check=True, timeout=60)
    changed = subprocess.run(
        ["git", "ls-files", "-m", "-o", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    for name in filter(None, changed.decode().split("\0")):
        source = ROOT / name
        if source.is_file():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, repo / name)
    deleted = subprocess.run(
        ["git", "ls-files", "-d", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    for name in filter(None, deleted.decode().split("\0")):
        (repo / name).unlink(missing_ok=True)
    canaries = {}
    for rel in POISON:
        canary = f"CANARY-{uuid.uuid4().hex}"
        canaries[rel] = canary
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{canary}\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "add", "-f", "--", *POISON)
    _git(repo, "commit", "-q", "-m", "base")
    (repo.parent / "canaries.txt").write_text("\n".join(canaries.values()), encoding="utf-8")
    return repo


@pytest.fixture
def repo(base_repo: Path, tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(base_repo, target, symlinks=True)
    return target


@pytest.fixture
def canaries(base_repo: Path) -> list[str]:
    return (base_repo.parent / "canaries.txt").read_text(encoding="utf-8").split("\n")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(STUB_DOCKER, encoding="utf-8")
    stub.chmod(0o755)
    fake_tar = tmp_path / "fake-image.tar"
    with tarfile.open(fake_tar, "w") as tar:
        manifest = tmp_path / "manifest.json"
        manifest.write_text('[{"Config":"c.json","RepoTags":["procurement-agent:0.1.0"]}]')
        tar.add(manifest, arcname="manifest.json")
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_IMAGE_TAR": str(fake_tar),
        "SOURCE_DATE_EPOCH": EPOCH,
    }


def _make(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["bash", SCRIPT, *args], repo, env)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _version(repo: Path) -> str:
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'^version = "([^"]+)"', text, re.M)
    assert found
    return found.group(1)


def _bundle(repo: Path) -> Path:
    found = sorted((repo / "dist").glob("procurement-agent-*"))
    assert len(found) == 1, found
    return found[0]


def _files(bundle: Path) -> list[str]:
    return sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file())


def _commit_all(repo: Path, message: str = "change") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _release(bundle: Path) -> dict[str, str]:
    pairs = {}
    for line in (bundle / "RELEASE.txt").read_text(encoding="utf-8").splitlines():
        if ": " in line and not line.startswith((" ", "#")):
            key, value = line.split(": ", 1)
            pairs[key] = value
    return pairs


# --- the script itself ---------------------------------------------------------------------------


def test_the_script_is_executable_strict_and_documents_its_use() -> None:
    path = ROOT / SCRIPT
    assert os.access(path, os.X_OK)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in text
    header = text.split("set -euo pipefail")[0]
    for flag in ("--image", "--no-image", "--commit", "--out-dir"):
        assert flag in header
    assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0


def test_shellcheck_is_clean_where_it_is_installed() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed here; CI-less check, run it by hand")
    done = _run(["shellcheck", "--shell=bash", SCRIPT], ROOT)
    assert done.returncode == 0, done.stdout


def test_the_allow_list_is_printed_and_is_the_one_this_test_expects(
    repo: Path, env: dict[str, str]
) -> None:
    done = _make(repo, env, "--list-allow")
    assert done.returncode == 0, done.stderr
    assert sorted(done.stdout.split()) == ALLOWED_FROM_REPO


# --- what is in a bundle -------------------------------------------------------------------------


def test_a_bundle_without_an_image_holds_exactly_the_allow_list_and_what_it_writes(
    repo: Path, env: dict[str, str]
) -> None:
    done = _make(repo, env, "--no-image")
    assert done.returncode == 0, done.stderr
    bundle = _bundle(repo)
    assert bundle.name == f"procurement-agent-{_version(repo)}-{_head(repo)[:12]}"
    assert _files(bundle) == sorted([*ALLOWED_FROM_REPO, *GENERATED])


def test_every_copied_file_is_the_committed_one_byte_for_byte(
    repo: Path, env: dict[str, str]
) -> None:
    assert _make(repo, env, "--no-image").returncode == 0
    bundle = _bundle(repo)
    for rel in ALLOWED_FROM_REPO:
        committed = subprocess.run(
            ["git", "show", f"HEAD:{rel}"], cwd=repo, capture_output=True, check=True
        ).stdout
        assert (bundle / rel).read_bytes() == committed, rel


def test_nothing_from_a_denied_path_gets_in_even_when_it_is_tracked(
    repo: Path, env: dict[str, str], canaries: list[str]
) -> None:
    assert len(canaries) == len(POISON)
    assert _make(repo, env, "--no-image").returncode == 0
    bundle = _bundle(repo)
    for rel in _files(bundle):
        assert not set(Path(rel).parts) & DENIED_PARTS, rel
    blob = b"".join(p.read_bytes() for p in bundle.rglob("*") if p.is_file())
    for canary in canaries:
        assert canary.encode() not in blob, canary


def test_sha256sums_lists_every_other_file_and_verifies(repo: Path, env: dict[str, str]) -> None:
    assert _make(repo, env, "--no-image").returncode == 0
    bundle = _bundle(repo)
    lines = (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sorted(line.split("  ", 1)[1] for line in lines) == sorted(
        f for f in _files(bundle) if f != "SHA256SUMS"
    )
    for line in lines:
        digest, rel = line.split("  ", 1)
        assert digest == hashlib.sha256((bundle / rel).read_bytes()).hexdigest()
    assert _run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], bundle).returncode == 0
    (bundle / "README.md").write_text("tampered\n", encoding="utf-8")
    assert _run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], bundle).returncode != 0


def test_release_txt_records_version_commit_date_and_the_hashes(
    repo: Path, env: dict[str, str]
) -> None:
    assert _make(repo, env, "--no-image").returncode == 0
    bundle = _bundle(repo)
    release = _release(bundle)
    assert release["version"] == _version(repo)
    assert release["commit"] == _head(repo)
    assert release["tag"] == "none"
    assert release["date_utc"] == "2023-11-14T22:13:20Z"
    assert release["uv_lock_sha256"] == hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest()
    sbom = (bundle / "docs" / "sbom.cdx.json").read_bytes()
    assert release["sbom_sha256"] == hashlib.sha256(sbom).hexdigest()
    assert release["image_archive"].startswith("not included")
    assert "--no-image" in release["image_archive"]


def test_release_txt_names_the_tag_when_head_is_tagged(repo: Path, env: dict[str, str]) -> None:
    _git(repo, "tag", "v0.1.0")
    assert _make(repo, env, "--no-image").returncode == 0
    assert _release(_bundle(repo))["tag"] == "v0.1.0"


def test_the_same_commit_gives_the_same_release_txt(
    repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    assert _make(repo, env, "--no-image").returncode == 0
    first = (_bundle(repo) / "RELEASE.txt").read_bytes()
    assert _make(repo, env, "--no-image", "--out-dir", str(tmp_path / "again")).returncode == 0
    second = next((tmp_path / "again").glob("*/RELEASE.txt")).read_bytes()
    assert first == second


# --- images ---------------------------------------------------------------------------------------


def test_the_image_archive_is_docker_save_through_gzip_and_is_recorded(
    repo: Path, env: dict[str, str]
) -> None:
    done = _make(repo, env, "--image", "procurement-agent:0.1.0", "--image", "ollama/ollama:0.34.2")
    assert done.returncode == 0, done.stderr
    bundle = _bundle(repo)
    archive = f"procurement-agent-{_version(repo)}-image.tar.gz"
    assert _files(bundle) == sorted([*ALLOWED_FROM_REPO, *GENERATED, archive])
    unpacked = subprocess.run(
        ["gzip", "-dc", str(bundle / archive)], capture_output=True, check=True
    ).stdout
    assert unpacked == Path(env["FAKE_IMAGE_TAR"]).read_bytes()
    release = (bundle / "RELEASE.txt").read_text(encoding="utf-8")
    assert f"image_archive: {archive}" in release
    assert "procurement-agent:0.1.0" in release and "ollama/ollama:0.34.2" in release
    assert "sha256:" + "7".zfill(64) in release  # the image id
    assert "12345" in release  # its size
    assert "ollama/ollama@sha256:" + "5".zfill(64) in release  # a repo digest, when there is one
    assert _run(["sha256sum", "-c", "--quiet", "SHA256SUMS"], bundle).returncode == 0
    assert archive in (bundle / "SHA256SUMS").read_text(encoding="utf-8")


def test_an_image_docker_does_not_have_is_refused_and_nothing_is_left(
    repo: Path, env: dict[str, str]
) -> None:
    done = _make(repo, env, "--image", "procurement-agent:9.9.9", "--image", "nosuch:1")
    assert done.returncode != 0
    assert "nosuch:1" in done.stderr
    assert not (repo / "dist").exists() or not list((repo / "dist").iterdir())


def test_install_md_says_which_compose_images_the_archive_holds_and_which_it_does_not(
    repo: Path, env: dict[str, str]
) -> None:
    assert _make(repo, env, "--image", "procurement-agent:0.1.0").returncode == 0
    install = (_bundle(repo) / "INSTALL.md").read_text(encoding="utf-8")
    compose = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    refs = re.findall(r"^\s+image:\s*(\S+)", compose, re.M)
    assert len(refs) == 3
    for ref in refs:
        assert ref in install
    assert re.search(r"in the archive:\s+`procurement-agent:0\.1\.0`", install)
    assert re.search(r"NOT in the archive[^\n]*\n(?:[^\n]*\n)*?[^\n]*ollama/ollama", install)


# --- refusals -------------------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["modify", "untracked", "staged"])
def test_a_dirty_tree_is_refused(repo: Path, env: dict[str, str], change: str) -> None:
    if change == "modify":
        (repo / "README.md").write_text("edited\n", encoding="utf-8")
    elif change == "untracked":
        (repo / "notes.txt").write_text("x\n", encoding="utf-8")
    else:
        (repo / "README.md").write_text("edited\n", encoding="utf-8")
        _git(repo, "add", "README.md")
    done = _make(repo, env, "--no-image")
    assert done.returncode == 1
    assert "not clean" in done.stderr
    assert not (repo / "dist").exists()


def test_an_ignored_file_does_not_make_the_tree_dirty(repo: Path, env: dict[str, str]) -> None:
    (repo / ".venv").mkdir()
    (repo / ".venv" / "x").write_text("x\n", encoding="utf-8")
    assert _make(repo, env, "--no-image").returncode == 0


def test_commit_must_name_head(repo: Path, env: dict[str, str]) -> None:
    head = _head(repo)
    assert _make(repo, env, "--no-image", "--commit", head).returncode == 0
    shutil.rmtree(repo / "dist")
    assert _make(repo, env, "--no-image", "--commit", head[:9]).returncode == 0
    shutil.rmtree(repo / "dist")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "later")
    done = _make(repo, env, "--no-image", "--commit", head)
    assert done.returncode == 1
    assert "HEAD is not" in done.stderr
    assert not (repo / "dist").exists()
    bogus = _make(repo, env, "--no-image", "--commit", "deadbeef")
    assert bogus.returncode == 1


@pytest.mark.parametrize(
    "args",
    [[], ["--no-image", "--image", "procurement-agent:0.1.0"], ["--bogus"], ["--image"]],
)
def test_the_image_choice_has_to_be_explicit(
    repo: Path, env: dict[str, str], args: list[str]
) -> None:
    done = _make(repo, env, *args)
    assert done.returncode == 2
    assert "usage" in done.stderr.lower()


def test_an_existing_bundle_is_never_overwritten(repo: Path, env: dict[str, str]) -> None:
    assert _make(repo, env, "--no-image").returncode == 0
    marker = _bundle(repo) / "marker"
    marker.write_text("keep\n", encoding="utf-8")
    done = _make(repo, env, "--no-image")
    assert done.returncode == 1 and "already exists" in done.stderr
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_a_stale_sbom_or_inventory_is_refused(repo: Path, env: dict[str, str]) -> None:
    sbom = repo / "docs" / "sbom.cdx.json"
    original = sbom.read_text(encoding="utf-8")
    sbom.write_text(
        re.sub(r'("pa:uv-lock-sha256",\s+"value": ")[0-9a-f]{64}', r"\g<1>" + "0" * 64, original),
        encoding="utf-8",
    )
    _commit_all(repo, "stale sbom")
    done = _make(repo, env, "--no-image")
    assert done.returncode == 1
    assert "sbom" in done.stderr.lower() and "uv.lock" in done.stderr
    sbom.write_text(original, encoding="utf-8")
    third = repo / "docs" / "THIRD_PARTY.md"
    third.write_text(
        third.read_text(encoding="utf-8").replace("SHA-256 `", "SHA-256 `0", 1), encoding="utf-8"
    )
    _commit_all(repo, "stale inventory")
    done = _make(repo, env, "--no-image")
    assert done.returncode == 1
    assert "THIRD_PARTY" in done.stderr


def test_a_missing_allow_listed_file_is_refused(repo: Path, env: dict[str, str]) -> None:
    _git(repo, "rm", "-q", "docs/SECURITY.md")
    _git(repo, "commit", "-q", "-m", "remove")
    done = _make(repo, env, "--no-image")
    assert done.returncode == 1
    assert "docs/SECURITY.md" in done.stderr


# --- INSTALL.md -----------------------------------------------------------------------------------


@pytest.fixture
def install(repo: Path, env: dict[str, str]) -> str:
    assert _make(repo, env, "--no-image").returncode == 0
    return (_bundle(repo) / "INSTALL.md").read_text(encoding="utf-8")


def test_install_md_gives_the_offline_steps_in_order(install: str) -> None:
    headings = re.findall(r"^## \d+\. (.+)$", install, re.M)
    keywords = [
        "Verify",
        "Load the images",
        "Models",
        "Configuration",
        "Start",
        "first API key",
        "Reference data",
        "Reverse proxy",
        "Backups",
        "Upgrade",
    ]
    positions = [
        next(i for i, h in enumerate(headings) if k.lower() in h.lower()) for k in keywords
    ]
    assert positions == sorted(positions) and len(set(positions)) == len(keywords)


def test_install_md_says_there_is_no_image_when_there_is_none(install: str) -> None:
    assert "no image archive" in install.lower()
    assert "--no-image" in install


def test_install_md_says_the_project_name_is_fixed_and_carries_state_through_an_upgrade(
    install: str,
) -> None:
    assert "name: procurement-agent" in install
    assert "COMPOSE_PROJECT_NAME=procurement-agent" not in install, "the compose file fixes it"
    upgrade = install.split("## 10. Upgrade")[1]
    assert ".env" in upgrade and "models" in upgrade
    assert "docker compose down" in upgrade
    assert "-v" not in re.findall(r"docker compose down[^\n]*", upgrade)[0].split()


def test_install_md_commands_are_the_ones_the_real_docs_give(install: str) -> None:
    docs = (ROOT / "docs" / "PRODUCTION_READINESS.md").read_text(encoding="utf-8")
    docs += (ROOT / "README.md").read_text(encoding="utf-8")
    verbatim = [
        "docker compose exec app python -m procurement_agent.cli create-key",
        "docker compose exec app python -m procurement_agent.cli load-fx-rates /data/nbg.json",
        "docker compose exec app rm /data/nbg.json",
        "docker compose stop proxy app",
        "python -m procurement_agent.cli models-manifest write",
    ]
    for command in verbatim:
        assert command in install, command
        assert command in docs.replace("\\\n  ", " ").replace("\n    ", " "), command
    backup = re.search(r"docker compose exec -T app python -c \"[^\n]+", install)
    assert backup is not None
    assert backup.group(0).split(" > ")[0] in docs


def test_the_cli_commands_install_md_names_exist() -> None:
    cli = (ROOT / "src" / "procurement_agent" / "cli.py").read_text(encoding="utf-8")
    for name in ("create-key", "load-fx-rates", "load-registry", "models-manifest"):
        assert f'"{name}"' in cli, name


def test_a_setting_is_listed_only_when_the_example_has_it(repo: Path, env: dict[str, str]) -> None:
    assert _make(repo, env, "--no-image").returncode == 0
    install = (_bundle(repo) / "INSTALL.md").read_text(encoding="utf-8")
    assert "PA_ALLOWED_HOSTS" in install
    assert "PA_PARSER" not in install  # .env.example has no such setting
    shutil.rmtree(repo / "dist")
    example = repo / ".env.example"
    example.write_text(
        example.read_text(encoding="utf-8") + "PA_PARSER=docling\n", encoding="utf-8"
    )
    _commit_all(repo, "add a parser setting")
    assert _make(repo, env, "--no-image").returncode == 0
    assert "PA_PARSER=docling" in (_bundle(repo) / "INSTALL.md").read_text(encoding="utf-8")


def test_install_md_mentions_no_path_outside_the_bundle_that_it_asks_you_to_run(
    install: str,
) -> None:
    # commands run from the bundle directory; the only repository path named is a checkout
    # on the connected machine
    assert "scripts/fetch_nbg_rates.py" in install
    assert "checkout" in install
