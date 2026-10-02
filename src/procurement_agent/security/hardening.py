"""Process-wide settings that keep a crash or a stray file from becoming a copy of the data.

* ``umask(0o077)``: every file this process creates is owner-only, whichever code created it - the
  database's write-ahead log, a library's temporary file, a result written without an explicit
  mode. The files written on purpose are also given a mode; this is for the ones nobody thought of.
* ``RLIMIT_CORE = 0``: a crash in a native parser (pdfium, Tesseract, a tokenizer) would otherwise
  dump the process's memory - the text of the documents being read - into a core file, in the
  working directory or wherever ``core_pattern`` sends it. The container sets ``ulimits.core: 0``
  as well (docker-compose.yml); this covers a process started by hand.

Both are restored by the callable ``harden_process`` returns, so a test suite (which starts the
application hundreds of times in one process) leaves the interpreter as it found it.
"""

from __future__ import annotations

import contextlib
import os
import resource
from collections.abc import Callable

__all__ = ["harden_process"]


def harden_process() -> Callable[[], None]:
    """Apply the settings; the returned function puts back what was there before."""
    previous_umask = os.umask(0o077)
    previous_core = resource.getrlimit(resource.RLIMIT_CORE)
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_CORE, (0, previous_core[1]))

    def restore() -> None:
        os.umask(previous_umask)
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_CORE, previous_core)

    return restore
