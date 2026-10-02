"""Offline mode is pinned by the package itself, whichever module an entry point imports first.

``huggingface_hub`` evaluates ``HF_HUB_OFFLINE`` once, when it is first imported, and docling
imports it. The flags used to be set by ``procurement_agent.config``, so a module that imported
docling first (the evaluation runner, the document service, the pipeline) ran with the library
online - measured - and a value already in the environment (an ``env_file``, a shell profile)
always won over the default. Each scenario runs in a fresh interpreter: the constant is read once.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap

import pytest

import procurement_agent

_PROBE = textwrap.dedent(
    """
    import importlib, os, sys
    importlib.import_module(sys.argv[1])
    import huggingface_hub.constants as hub
    print(hub.HF_HUB_OFFLINE, os.environ["HF_HUB_OFFLINE"], os.environ["TRANSFORMERS_OFFLINE"],
          os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"], os.environ["HF_HUB_DISABLE_TELEMETRY"])
    """
)


def _probe(module: str, **env: str) -> list[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("HF_", "TRANSFORMERS_"))}
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**base, **env},
    )
    assert done.returncode == 0, done.stderr[-800:]
    return done.stdout.split()


@pytest.mark.parametrize(
    ("module", "env"),
    [
        # the entry points that imported docling before the configuration: flags unset
        ("procurement_agent.eval.runner", {}),
        # an environment that tries to switch the network back on
        (
            "procurement_agent.ingestion.docling_service",
            {"HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0"},
        ),
        (
            "procurement_agent.extraction.pipeline",
            {"HF_HUB_OFFLINE": "false", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "0"},
        ),
    ],
)
def test_hub_is_offline_whichever_module_is_imported_first(
    module: str, env: dict[str, str]
) -> None:
    assert _probe(module, **env) == ["True", "1", "1", "1", "1"]


def test_the_pins_are_assigned_not_defaulted(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in procurement_agent._PINNED:
        monkeypatch.setenv(name, "0")
    importlib.reload(procurement_agent)
    assert all(os.environ[name] == "1" for name in procurement_agent._PINNED)


def test_a_performance_setting_is_still_the_callers_to_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "true")
    importlib.reload(procurement_agent)
    assert os.environ["TOKENIZERS_PARALLELISM"] == "true"
