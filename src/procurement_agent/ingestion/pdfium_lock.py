"""One lock for every use of pdfium that this application makes itself.

pypdfium2's documentation is plain about it: PDFium is inherently not thread-safe, and calling it
from two threads at once is not allowed - not even for two different documents. Three places in the
request path use it (the upload guard counting pages, the page-image renderer, the scanned-PDF
check in the segmenter), each from whichever thread the web server handed the request to, so two
users uploading and viewing PDFs at the same time were two threads in pdfium.

Each of those holds this lock for the few milliseconds it touches pdfium. What it cannot cover is
Docling, whose PDF backend calls pdfium from the job worker's thread for as long as a conversion
takes (minutes): serialising against that would make every upload and page view wait for it. That
overlap is the reason the plan names a parser sandbox - parsing in a child process, with its own
copy of the library - as the structural fix (docs/SECURITY.md, "Known gaps").
"""

from __future__ import annotations

import threading

__all__ = ["PDFIUM_LOCK"]

#: Reentrant: the segmenter's check runs inside code that may already hold it.
PDFIUM_LOCK = threading.RLock()
