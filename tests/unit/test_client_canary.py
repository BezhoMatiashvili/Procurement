"""The client-identifier canary: harvest once, refuse commits and pushes that carry an identifier.

Every identifier below is invented. The tool must never print or store one in the clear.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "client_canary.py"
_spec = importlib.util.spec_from_file_location("client_canary", _SCRIPT)
assert _spec is not None and _spec.loader is not None
canary: ModuleType = importlib.util.module_from_spec(_spec)
sys.modules["client_canary"] = canary
_spec.loader.exec_module(canary)

TAX_ID = "987654321"
PERSONAL_ID = "12345678901"
IBAN = "GE29NB0000000101904917"
EMAIL = "ana.tester@example.invalid"
ORG = "ბეტა ჯგუფი"
COMMON = "ბანკი"  # an ordinary word, as the dictionary below says
PERSON = "გიორგი ტესტაძე"
CLIENT_TEXT = (
    f"კომპანია შპს „{ORG}“, საიდენტიფიკაციო კოდი {TAX_ID}, პირადი ნომერი {PERSONAL_ID}. "
    f"ანგარიში {IBAN}. ელფოსტა {EMAIL}, ტელეფონი +995 555 12 34 56. "
    f"მეორე მხარე შპს „{COMMON}“. დირექტორი ი/მ {PERSON}. "
)
PROSE = " ".join(f"სიტყვა{i}" for i in range(40))  # forty words nobody else has written
DICTIONARY = frozenset({COMMON})


def _store() -> object:
    return canary.harvest_texts([CLIENT_TEXT, PROSE], dictionary=DICTIONARY)


def _scan(text: str, store: object | None = None) -> list[str]:
    found = canary.scan_lines("f.txt", list(enumerate(text.splitlines(), 1)), store or _store())
    return [f.kind for f in found]


def test_the_store_holds_hashes_never_the_identifiers() -> None:
    stored = json.loads(_store().to_json())
    entries = [*stored["hard"], *stored["soft"], *stored["runs"]]
    assert entries
    assert all(re.fullmatch(r"[0-9a-f]{16}", entry) for entry in entries)  # no plaintext entry
    blob = json.dumps(stored, ensure_ascii=False)
    assert not re.search(r"[\u10A0-\u10FF@]", blob)  # no Georgian text, no e-mail address
    assert "GE29" not in blob
    assert stored["salt"] and set(stored) == {"version", "salt", "hard", "soft", "runs", "counts"}


def test_two_harvests_use_different_salts() -> None:
    a = canary.harvest_texts([CLIENT_TEXT])
    b = canary.harvest_texts([CLIENT_TEXT])
    assert a.salt != b.salt and a.hard.isdisjoint(b.hard)


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (f"the supplier's tax id is {TAX_ID}.", "id"),
        (f"personal {PERSONAL_ID}", "id"),
        (f"pay to {IBAN}", "iban"),
        ("pay to " + " ".join(IBAN[i : i + 4] for i in range(0, len(IBAN), 4)), "iban"),
        (f"write to {EMAIL.upper()}", "email"),
        ("call 555-12-34-56", "phone"),
        ("call +995555123456", "phone"),
        (f"შპს „{ORG}“ signed", "name"),
        (f"the {ORG} contract", "name"),
        (f"ი/მ {PERSON}", "name"),
    ],
)
def test_each_kind_of_identifier_is_found(line: str, kind: str) -> None:
    assert kind in _scan(line)


def test_a_name_that_is_an_ordinary_word_only_warns() -> None:
    [found] = canary.scan_lines("f.txt", [(1, f"a {COMMON} account")], _store())
    assert found.kind == "name~soft" and not found.hard


def test_text_without_identifiers_is_clean() -> None:
    assert _scan("an ordinary sentence about 123 apples and 4,500.00 GEL, contract 15/2026") == []


def test_numbers_that_only_contain_an_id_are_not_the_id() -> None:
    assert _scan(f"total {TAX_ID}00 and 1,{TAX_ID}") == []


def test_a_run_of_twenty_five_words_of_client_text_is_a_hard_hit() -> None:
    words = PROSE.split()
    assert "text" not in _scan(" ".join(words[:24]))
    assert "text" in _scan(" ".join(words[:25]))
    assert "text" in _scan("lead in " + " ".join(words[5:35]) + " tail")


def test_a_reviewed_public_name_can_be_demoted_to_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canary, "CONFIG_DIR", tmp_path / "canary")
    canary.save_store(_store())
    [hit] = canary.scan_lines("f.txt", [(1, f"the {ORG} contract")], canary.load_store())
    assert hit.kind == "name" and hit.hard and f"[#{hit.ref}]" in str(hit)
    assert canary.cmd_allow(argparse.Namespace(refs=[f"#{hit.ref}"])) == 0
    [after] = canary.scan_lines("f.txt", [(1, f"the {ORG} contract")], canary.load_store())
    assert after.kind == "name~soft" and not after.hard
    assert canary.cmd_allow(argparse.Namespace(refs=["zzzzzz"])) == 1  # an unknown reference


def test_added_lines_carry_their_new_line_numbers() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,0 +1,2 @@\n+first\n+second\n"
        "@@ -10,2 +12,1 @@\n-gone\n-gone too\n+kept\n"
        "diff --git a/b.md b/b.md\n--- /dev/null\n+++ b/b.md\n@@ -0,0 +1 @@\n+new file\n"
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-removed\n"
    )
    assert canary.added_lines(diff) == {
        "a.py": [(1, "first"), (2, "second"), (12, "kept")],
        "b.md": [(1, "new file")],
    }


def test_a_finding_names_the_place_and_the_kind_only() -> None:
    diff = f"--- a/n.md\n+++ b/n.md\n@@ -0,0 +4 @@\n+tax id {TAX_ID}\n"
    [finding] = canary.scan_diff(diff, _store())
    assert str(finding) == "n.md:4: client id" and TAX_ID not in str(finding)


# --- the installed hooks, in a real throwaway repository ---------------------------------------


def _git(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, env=env
    )


@pytest.fixture
def hooked_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    config = tmp_path / "canary"
    monkeypatch.setattr(canary, "CONFIG_DIR", config)
    canary.save_store(_store())
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    canary.cmd_install(argparse.Namespace(git_dir=str(repo / ".git")))
    env = {**os.environ, "PA_CANARY_DIR": str(config)}
    return repo, env


def test_a_commit_that_adds_an_identifier_is_refused_without_showing_it(
    hooked_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = hooked_repo
    (repo / "ok.md").write_text("nothing to see\n")
    _git(repo, "add", "ok.md")
    assert _git(repo, "commit", "-m", "clean", env=env).returncode == 0

    (repo / "notes.md").write_text(f"line one\nthe tax id is {TAX_ID}\n")
    _git(repo, "add", "notes.md")
    refused = _git(repo, "commit", "-m", "leak", env=env)
    assert refused.returncode != 0
    assert "notes.md:2: client id" in refused.stderr
    assert TAX_ID not in refused.stdout + refused.stderr
    assert _git(repo, "commit", "--no-verify", "-m", "deliberate", env=env).returncode == 0


def test_documents_are_refused_outside_the_fixtures(
    hooked_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = hooked_repo
    (repo / "tests" / "fixtures").mkdir(parents=True)
    (repo / "tests" / "fixtures" / "synthetic.docx").write_bytes(b"PK")
    (repo / "contract.pdf").write_bytes(b"%PDF-")
    _git(repo, "add", "tests/fixtures/synthetic.docx")
    assert _git(repo, "commit", "-m", "fixture", env=env).returncode == 0
    _git(repo, "add", "contract.pdf")
    refused = _git(repo, "commit", "-m", "document", env=env)
    assert refused.returncode != 0 and "contract.pdf" in refused.stderr


def _with_the_generic_rule(repo: Path) -> None:
    """Give the throwaway repository the script a real checkout has (the hook finds it by path)."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_no_documents.sh"
    target = repo / "scripts" / "check_no_documents.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(script.read_bytes())
    target.chmod(0o755)
    _git(repo, "add", "scripts/check_no_documents.sh")
    _git(repo, "commit", "-q", "--no-verify", "-m", "the generic rule")


@pytest.mark.parametrize(
    "name",
    ["app.db", "server.pem", ".env", "sub/.env.production", "data/uploads.txt", "models/x.bin"],
)
def test_the_hook_runs_the_whole_generic_rule_when_the_checkout_has_it(
    hooked_repo: tuple[Path, dict[str, str]], name: str
) -> None:
    """A database, a key, an environment file or a client directory is not a "document" by
    extension; before the hook ran the script, only CI (after the push) refused them."""
    repo, env = hooked_repo
    _with_the_generic_rule(repo)
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    _git(repo, "add", "-f", name)
    refused = _git(repo, "commit", "-m", "x", env=env)
    assert refused.returncode != 0, name
    assert name in refused.stderr


def test_the_hook_refuses_a_link_to_a_client_directory(
    hooked_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = hooked_repo
    _with_the_generic_rule(repo)
    (tmp_path / "elsewhere").mkdir()
    (repo / "samples").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    _git(repo, "add", "-f", "samples")
    refused = _git(repo, "commit", "-m", "x", env=env)
    assert refused.returncode != 0 and "samples" in refused.stderr


def test_an_ordinary_commit_passes_the_generic_rule(
    hooked_repo: tuple[Path, dict[str, str]],
) -> None:
    repo, env = hooked_repo
    _with_the_generic_rule(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hi')\n")
    (repo / ".env.example").write_text("PA_ENV=dev\n")
    _git(repo, "add", "src/app.py", ".env.example")
    assert _git(repo, "commit", "-m", "ordinary", env=env).returncode == 0


def test_a_broken_script_is_not_a_refusal(hooked_repo: tuple[Path, dict[str, str]]) -> None:
    """The hook must never be what stops an ordinary commit: a script that exits 2 is ignored."""
    repo, env = hooked_repo
    target = repo / "scripts" / "check_no_documents.sh"
    target.parent.mkdir(parents=True)
    target.write_text("#!/usr/bin/env bash\nexit 2\n")
    target.chmod(0o755)
    (repo / "ok.md").write_text("fine\n")
    _git(repo, "add", "ok.md")
    assert _git(repo, "commit", "-m", "ordinary", env=env).returncode == 0


def test_a_push_that_carries_an_identifier_is_refused(
    hooked_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, env = hooked_repo
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "notes.md").write_text(f"the tax id is {TAX_ID}\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "--no-verify", "-m", "slipped past", env=env)
    (repo / "notes.md").write_text("clean now\n")  # gone from the tip, still in the history
    _git(repo, "commit", "-a", "-m", "removed", env=env)
    pushed = _git(repo, "push", "origin", "HEAD:main", env=env)
    assert pushed.returncode != 0 and "client id" in pushed.stderr
    assert TAX_ID not in pushed.stdout + pushed.stderr
    assert (
        subprocess.run(
            ["git", "-C", str(remote), "rev-parse", "--verify", "-q", "main"], capture_output=True
        ).returncode
        != 0
    )  # nothing reached the remote


def test_without_a_token_file_the_hooks_warn_and_let_the_commit_through(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    empty = tmp_path / "nothing-here"
    canary.CONFIG_DIR = empty  # type: ignore[misc]
    try:
        canary.cmd_install(argparse.Namespace(git_dir=str(repo / ".git")))
    finally:
        canary.CONFIG_DIR = Path(
            os.environ.get("PA_CANARY_DIR", str(Path.home() / ".config" / "procurement-canary"))
        )
    env = {**os.environ, "PA_CANARY_DIR": str(empty)}
    (repo / "a.md").write_text(f"{TAX_ID}\n")
    _git(repo, "add", "a.md")
    done = _git(repo, "commit", "-m", "x", env=env)
    assert done.returncode == 0 and "no token file" in done.stderr
