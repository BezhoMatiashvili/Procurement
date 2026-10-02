"""Procurement Agent: local, air-gapped document intelligence for procurement.

Importing this package pins every library the project uses into offline mode, and it has to
happen here, before anything else is imported: ``huggingface_hub`` evaluates ``HF_HUB_OFFLINE``
once, when it is first imported, and docling pulls it in. A module that imported docling before
``procurement_agent.config`` - the evaluation runner, the document service, the pipeline - used
to run with the library online, and the web server was right only because one of its modules
happened to import the configuration first.

The values are assigned, not defaulted. A ``setdefault`` lets whatever is already in the
environment win - an ``env_file`` of the container, a shell profile, a ``HF_HUB_OFFLINE=0`` left
over from downloading models - and the one thing these variables are for is that nothing can
switch the network back on. ``scripts/prefetch_models.sh`` downloads models in a process that never
imports this package.
"""

from __future__ import annotations

import os

#: Offline and telemetry switches, each forced to "1". ``HF_HUB_DISABLE_IMPLICIT_TOKEN`` keeps a
#: Hugging Face token stored in the home directory from being sent should a request ever be tried.
_PINNED = (
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HF_DATASETS_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN",
    "HF_HUB_DISABLE_UPDATE_CHECK",
    "DO_NOT_TRACK",
)
for _name in _PINNED:
    os.environ[_name] = "1"
#: A performance setting, not a promise: a caller may still choose otherwise.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
