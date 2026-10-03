#!/usr/bin/env python
"""Break each security property of a COPY of the source, one at a time; the tests must notice.

A security test that passes proves nothing until it is shown to fail when the thing it guards is
removed. This copies ``src/`` to a temporary directory, applies one mutation - ownership not
checked, authentication falling back to a dev user, the CSRF test always true, a key compared by
prefix only, the pre-body credential check removed, every host allowed, a header dropped, a
refusal that no longer closes the connection - runs the security tests against that copy, and
reports whether they failed (the mutation was CAUGHT) or passed (it SURVIVED: a gap in the tests).

Every mutation is an exact text replacement that must match exactly once, so a refactor that moves
the code fails here loudly instead of quietly testing nothing. The unmodified copy must pass every
suite first (a baseline), and only a test FAILURE (pytest exit 1) counts as a catch: an exit that
means the tests did not run - the mutated copy does not import, nothing was collected - is an ERROR.

One thing a catch does not mean in reverse: a test that starts its own interpreter and puts the
real ``src/`` on its path (the log-canary flow, the offline smoke, the real-server supervisor test)
exercises the UNMUTATED code whatever is mutated here. Such a test can only make a mutation
survive, never be caught, so a survivor may mean "tested only in a subprocess".

    python scripts/mutate_security.py            # every mutation
    python scripts/mutate_security.py --only owned_document
"""

# The mutations are exact text from the source and read best on one line each.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: The tests that stand guard. A mutation is caught when any of them fails.
SUITES = [
    "tests/integration/test_security_matrix.py",
    "tests/integration/test_hardening_web.py",
    "tests/integration/test_api.py",
    "tests/integration/test_preauth.py",
    "tests/integration/test_headers_and_cookies.py",
    "tests/integration/test_network_policy.py",
    "tests/unit/test_hosts.py",
    "tests/unit/test_network_settings.py",
    "tests/unit/test_llm_policy.py",
    "tests/unit/test_upload_guard.py",
]


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    why: str
    #: the tests that guard this one control; empty = every suite in ``SUITES``
    suites: tuple[str, ...] = ()


MUTATIONS = [
    # --- what the acceptance-act work added: imported and typed rates, registry extracts, the
    # --- annotated files
    Mutation(
        "rates_quantity_unchecked",
        "src/procurement_agent/fx/rates_table.py",
        "if not isinstance(value, int) or value not in QUANTITIES:",
        "if not isinstance(value, int):",
        "a rate file's per-unit quantity is not checked: a rate per 100 units is read per unit",
        ("tests/unit/test_fx_rates_table.py",),
    ),
    Mutation(
        "rates_unbounded",
        "src/procurement_agent/fx/rates_table.py",
        "if not rate.is_finite() or rate <= 0 or rate >= _MAX_RATE:",
        "if not rate.is_finite():",
        "a rate file may carry a zero, negative or absurd rate into an invoice",
        ("tests/unit/test_fx_rates_table.py",),
    ),
    Mutation(
        "typed_rate_any_currency",
        "src/procurement_agent/api/checks.py",
        'currency: str = Field(pattern=r"^[A-Z]{3}$")',
        "currency: str = Field(max_length=64)",
        "a typed rate is taken for any currency text",
        ("tests/integration/test_checks_api.py",),
    ),
    Mutation(
        "typed_rate_unbounded",
        "src/procurement_agent/api/checks.py",
        "rate: Decimal = Field(gt=0, lt=1_000_000, max_digits=14, decimal_places=6)",
        "rate: Decimal = Field()",
        "a typed rate may be zero, negative or carry any precision",
        ("tests/integration/test_checks_api.py",),
    ),
    Mutation(
        "extract_without_title",
        "src/procurement_agent/registry/extract.py",
        "if not _TITLE.search(text):",
        "if False:",
        "any upload that names a party by its labels is trusted as a registry extract",
        ("tests/unit/test_registry_extract.py",),
    ),
    Mutation(
        "tracked_change_text_unsanitised",
        "src/procurement_agent/report/docx_annotate.py",
        'planned = tracked_edits(written, _XML_ILLEGAL.sub("", new))',
        "planned = tracked_edits(written, new)",
        "a correction's characters XML cannot carry reach the annotated act",
        ("tests/unit/test_docx_tracked_changes.py",),
    ),
    Mutation(
        "pdf_page_annotations_kept",
        "src/procurement_agent/report/pdf_annotate.py",
        "                _cleared(pages)\n",
        "",
        "an annotated PDF keeps the act's links and form fields, a launch or a script among them",
        ("tests/unit/test_pdf_annotate.py",),
    ),
    Mutation(
        "pdf_page_action_unchecked",
        "src/procurement_agent/report/pdf_annotate.py",
        "if any(pdfium_c.FPDF_GetPageAAction(page, kind) for kind in _PAGE_ACTIONS):",
        "if False:",
        "an annotated PDF keeps a page's own script, run when the page opens",
        ("tests/unit/test_pdf_annotate.py",),
    ),
    Mutation(
        "pdf_unused_objects_saved",
        "src/procurement_agent/report/pdf_annotate.py",
        "return _annotate(pdf, findings, out_path, text, summary, items)",
        "return _annotate(pages, findings, out_path, text, summary, items)",
        "an annotated PDF still carries the scripts of the annotations its pages no longer hold",
        ("tests/unit/test_pdf_annotate.py",),
    ),
    Mutation(
        "pdf_saved_copy_unchecked",
        "src/procurement_agent/report/pdf_annotate.py",
        "if active := active_names(out_path.read_bytes()):",
        "if active := set[str]():",
        "an annotated PDF goes out with a file attached to a form its page's content draws",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "extract_footer_backtracks",
        "src/procurement_agent/registry/extract.py",
        "(?:გვერდი[ \\t]*(?::[ \\t]*)?)?",
        "(?:გვერდი[ \\t]*:?[ \\t]*)?",
        "an extract with a page word followed by blanks holds the process for minutes",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "extract_syntax_error_uncaught",
        "src/procurement_agent/registry/extract.py",
        "except (ValueError, KeyError, zipfile.BadZipFile, PackageNotFoundError, SyntaxError) as e:",
        "except (ValueError, KeyError, zipfile.BadZipFile, PackageNotFoundError) as e:",
        "a Word extract that is not well-formed turns the act's check into a server error",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "docx_cells_per_grid_column",
        "src/procurement_agent/report/docx_annotate.py",
        "    for tr in tbl._tbl.tr_lst:\n        for tc in tr.tc_lst:\n            yield _Cell(tc, tbl)\n",
        "    for row in tbl.rows:\n        yield from row.cells\n",
        "a Word cell is read once for every column it says it spans: gigabytes for one cell",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "docx_xml_illegal_kept",
        "src/procurement_agent/report/docx_annotate.py",
        '_XML_ILLEGAL = re.compile(r"[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\ud800-\\udfff\\ufffe\\uffff]")',
        '_XML_ILLEGAL = re.compile(r"[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]")',
        "half a character in a finding fails the whole annotated file",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "hyperlink_field_unchecked",
        "src/procurement_agent/report/docx_sanitize.py",
        '    if kind == "HYPERLINK":\n        return not _safe_field_link(instruction[name.end() :])\n',
        "",
        "a link field in the annotated file opens a file, a share or a program's scheme",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "mend_unbounded",
        "src/procurement_agent/ingestion/docling_service.py",
        "if len(textlines) < 2 or len(textlines) > MEND_MAX_CELLS or len(words) > MEND_MAX_WORDS:",
        "if len(textlines) < 2:",
        "a PDF page of any size has its touching cells mended, a search of its words each",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "docx_places_uncapped",
        "src/procurement_agent/report/docx_annotate.py",
        "for place in places[:MAX_PLACES_PER_FINDING]:",
        "for place in places:",
        "a value a file repeats thousands of times is marked in every place, each costlier",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "docx_named_place_guessed",
        "src/procurement_agent/report/docx_annotate.py",
        "        if len(chosen) > MAX_SEARCHED:\n",
        "        if False:\n",
        "a finding naming a place past what the search reads is marked on words it does not mean",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "pdf_places_uncapped",
        "src/procurement_agent/report/pdf_annotate.py",
        "for place in places[:MAX_PLACES_PER_FINDING]:",
        "for place in places:",
        "a value a PDF repeats hundreds of times is highlighted in every place",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "pdf_length_read_anywhere",
        "src/procurement_agent/report/pdf_annotate.py",
        'if text == "Length" and depth == 1 and arrays == 0:',
        'if text == "Length":',
        "a /Length inside a resource makes the check of the saved copy skip the objects after it",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "pdf_length_read_in_arrays",
        "src/procurement_agent/report/pdf_annotate.py",
        'if text == "Length" and depth == 1 and arrays == 0:',
        'if text == "Length" and depth == 1:',
        "a /Length in an array of a stream's dictionary makes the check skip the objects after it",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "pdf_stream_end_unchecked",
        "src/procurement_agent/report/pdf_annotate.py",
        'if not data.startswith(b"endstream", at):',
        "if False:",
        "a stream whose length does not end it is read past, not refused",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "extract_text_unbounded",
        "src/procurement_agent/registry/extract.py",
        'return "\\n".join(bound_text(line) for line in text.split("\\n"))',
        "return text",
        "an extract's text reaches its patterns with runs of any length",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "extract_persons_unbounded",
        "src/procurement_agent/registry/extract.py",
        "if _PERSONAL_NUMBER.search(ln)][:MAX_PERSONS]",
        "if _PERSONAL_NUMBER.search(ln)]",
        "an extract's every numbered line is a person, each named in every finding",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "mend_unbudgeted",
        "src/procurement_agent/ingestion/docling_service.py",
        "                if index.looked_at > MEND_MAX_WORK:\n                    return textlines\n",
        "",
        "a PDF page built so that every clump reads every word of its line is mended however long",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "hyperlink_computed_target_trusted",
        "src/procurement_agent/report/docx_sanitize.py",
        "    return local\n",
        "    return True\n",
        "a link field whose target another field computes is kept",
        ("tests/unit/test_security_review_aoa.py",),
    ),
    Mutation(
        "raw_pdfium_anywhere",
        "src/procurement_agent/ingestion/render.py",
        "import pypdfium2 as pdfium\n",
        "import pypdfium2 as pdfium\nimport pypdfium2.raw as pdfium_c  # noqa: F401\n",
        "PDFium's raw C bindings are imported outside the one module allowed them",
        ("tests/unit/test_egress_surface.py",),
    ),
    Mutation(
        "owned_document",
        "src/procurement_agent/api/deps.py",
        "if doc is None or doc.owner != principal.user or doc.status == DocumentStatus.deleted:",
        "if doc is None or doc.status == DocumentStatus.deleted:",
        "any user can read, delete or check any document by id",
    ),
    Mutation(
        "owned_job",
        "src/procurement_agent/api/deps.py",
        "if job is None or job.owner != principal.user:",
        "if job is None:",
        "any user can read any job and its result",
    ),
    Mutation(
        "deleted_documents_serve_results",
        "src/procurement_agent/api/deps.py",
        "if live_document and job.document.status == DocumentStatus.deleted:",
        "if False:",
        "a deleted document's result is still served",
    ),
    Mutation(
        "authenticate_falls_back_to_dev",
        "src/procurement_agent/security/auth.py",
        'raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")',
        'return Principal(user="dev", key_id=None)',
        "a request with no credential is signed in as a user",
    ),
    Mutation(
        "csrf_always_same_origin",
        "src/procurement_agent/security/auth.py",
        "def is_same_origin(request: Request) -> bool:\n",
        "def is_same_origin(request: Request) -> bool:\n    return True\n",
        "a signed-in browser can be driven from any other site",
    ),
    Mutation(
        "csrf_fail_open_without_origin",
        "src/procurement_agent/security/auth.py",
        "    if not origin:\n        return False\n",
        "    if not origin:\n        return True\n",
        "a request that says nothing about its origin is trusted",
    ),
    Mutation(
        "key_compared_by_prefix_only",
        "src/procurement_agent/security/auth.py",
        "if hmac.compare_digest(row.key_hash, digest):",
        "if True:",
        "any key sharing a key's first characters is that key",
    ),
    Mutation(
        "revoked_keys_still_work",
        "src/procurement_agent/security/auth.py",
        "select(ApiKey).where(ApiKey.prefix == key[:_PREFIX_LEN], ApiKey.revoked_at.is_(None))",
        "select(ApiKey).where(ApiKey.prefix == key[:_PREFIX_LEN])",
        "revoking a key does not stop it",
    ),
    Mutation(
        "expired_sessions_still_work",
        "src/procurement_agent/security/auth.py",
        "if row.revoked_at is not None or _aware(row.expires_at) <= now:",
        "if row.revoked_at is not None:",
        "a browser session never expires",
    ),
    Mutation(
        "idle_sessions_still_work",
        "src/procurement_agent/security/auth.py",
        "if now - last_use > timedelta(minutes=settings.ui_session_idle_minutes):",
        "if False:",
        "a browser left open stays signed in",
    ),
    Mutation(
        "preauth_gate_removed",
        "src/procurement_agent/security/preauth.py",
        '            await run_in_threadpool(_check, request, scope["app"].state.settings)',
        "            pass",
        "an upload body is read before its caller is known",
    ),
    Mutation(
        "body_cap_ignored_off_upload_routes",
        "src/procurement_agent/security/body_limit.py",
        "        if (method, path) in self.upload_routes:\n            return self.upload_max_bytes\n        return self.default_max_bytes",
        "        return self.upload_max_bytes",
        "every route may carry a 100 MB body",
    ),
    Mutation(
        "all_hosts_allowed",
        "src/procurement_agent/security/hosts.py",
        "        if host in self._exact:\n            return True\n",
        "        return True\n",
        "any Host header is served (DNS rebinding, tunnels)",
    ),
    Mutation(
        "frame_options_dropped",
        "src/procurement_agent/security/headers.py",
        '    "X-Frame-Options": "DENY",\n',
        "",
        "pages can be framed by any site",
    ),
    Mutation(
        "csp_script_unsafe_inline",
        "src/procurement_agent/security/headers.py",
        "script-src 'self'; ",
        "script-src 'self' 'unsafe-inline'; ",
        "inline script runs in the application's pages",
    ),
    Mutation(
        "session_cookie_not_httponly",
        "src/procurement_agent/ui/routes.py",
        "        cookie_name(settings),\n        token,\n        httponly=True,",
        "        cookie_name(settings),\n        token,\n        httponly=False,",
        "page script can read the session cookie",
    ),
    Mutation(
        "session_cookie_not_samesite",
        "src/procurement_agent/ui/routes.py",
        '        token,\n        httponly=True,\n        samesite="strict",',
        '        token,\n        httponly=True,\n        samesite="none",',
        "the browser sends the session cookie cross-site",
    ),
    Mutation(
        "upload_guard_accepts_macros",
        "src/procurement_agent/security/upload_guard.py",
        '            if any(m in name.lower() for m in _MACRO_MARKERS):\n                raise UploadRejectedError("documents containing macros are not accepted")',
        "            pass",
        "a macro-carrying package is stored",
    ),
    Mutation(
        "egress_guard_allows_all",
        "src/procurement_agent/security/egress.py",
        "        return ip.is_loopback or str(ip) in self._ips",
        "        return True",
        "an outbound connection to any address goes through",
    ),
    Mutation(
        "llm_url_any_host",
        "src/procurement_agent/config.py",
        "    if not allow_lan and not _is_local_host(host):",
        "    if False:",
        "document text can be sent to any private host",
    ),
    Mutation(
        "ollama_follows_redirects",
        "src/procurement_agent/llm/ollama_client.py",
        "follow_redirects=False,",
        "follow_redirects=True,",
        "a redirect from the model server carries the document to wherever it points",
        ("tests/unit/test_llm_policy.py", "tests/unit/test_egress_surface.py"),
    ),
    Mutation(
        "ollama_reads_proxy_environment",
        "src/procurement_agent/llm/ollama_client.py",
        "trust_env=False,",
        "trust_env=True,",
        "HTTP(S)_PROXY in the environment routes the document through a proxy",
        ("tests/unit/test_llm_policy.py", "tests/unit/test_egress_surface.py"),
    ),
    Mutation(
        "ollama_any_endpoint_allowed",
        "src/procurement_agent/llm/ollama_client.py",
        "if not same_server or (request.method, url.path) not in allowed:",
        "if False:",
        "the model client can call endpoints that download, delete or push models",
        ("tests/unit/test_llm_policy.py",),
    ),
    Mutation(
        "ollama_cloud_models_allowed",
        "src/procurement_agent/llm/ollama_client.py",
        '_REMOTE_MARKERS = ("remote_host", "remote_model")',
        "_REMOTE_MARKERS = ()",
        "a model that Ollama forwards to its hosted service is used for document text",
        ("tests/unit/test_llm_policy.py",),
    ),
    Mutation(
        "error_text_not_scrubbed",
        "src/procurement_agent/security/safe_errors.py",
        "        text = _kind(e)",
        "        text = str(e)",
        "an exception message (which can quote the document) reaches logs and stored errors",
        ("tests/unit/test_safe_errors.py", "tests/integration/test_log_canary.py"),
    ),
    Mutation(
        "export_formulas_live",
        "src/procurement_agent/extraction/export.py",
        "ws.append([cell_safe(v) for v in values])",
        "ws.append(list(values))",
        "a sheet name or label starting with '=' becomes a live formula in the export",
        ("tests/integration/test_export_formula_safety.py",),
    ),
    Mutation(
        "secure_delete_off",
        "src/procurement_agent/db.py",
        'cur.execute("PRAGMA secure_delete=ON")',
        'cur.execute("PRAGMA secure_delete=OFF")',
        "a deleted row's text stays readable in the database file",
        ("tests/unit/test_hardening.py",),
    ),
    Mutation(
        "umask_world_readable",
        "src/procurement_agent/security/hardening.py",
        "os.umask(0o077)",
        "os.umask(0o022)",
        "every file the process creates is readable by every local user",
        ("tests/unit/test_hardening.py",),
    ),
    Mutation(
        "model_tampering_unnoticed",
        "src/procurement_agent/security/models_manifest.py",
        "if _digest(path) != want:",
        "if False:",
        "a model file that was changed on disk is still loaded",
        ("tests/unit/test_models_manifest.py",),
    ),
    Mutation(
        "deletion_keeps_the_question",
        "src/procurement_agent/jobs/worker.py",
        "        j.params = {}  # a question the user typed about a document that no longer exists",
        "        pass",
        "a deleted document's question text stays in the job table",
        ("tests/unit/test_retention.py",),
    ),
    Mutation(
        "sweep_strands_files",
        "src/procurement_agent/jobs/worker.py",
        "        reconcile(self._s)\n        checkpoint()",
        "        checkpoint()",
        "files of a document whose deletion was interrupted stay on disk for good",
        ("tests/unit/test_retention.py",),
    ),
    Mutation(
        "audit_trail_rewritable",
        "src/procurement_agent/db.py",
        "        for ddl in _AUDIT_TRIGGERS.values():\n            conn.exec_driver_sql(ddl)",
        "        for ddl in ():\n            conn.exec_driver_sql(ddl)",
        "an audit row can be edited or deleted by the application",
        ("tests/integration/test_audit_trail.py",),
    ),
    Mutation(
        "session_cookie_without_host_prefix",
        "src/procurement_agent/security/auth.py",
        'return HOST_COOKIE_NAME if settings.env == "prod" else COOKIE_NAME',
        "return COOKIE_NAME",
        "a sibling sub-domain can overwrite the production session cookie",
        ("tests/integration/test_headers_and_cookies.py",),
    ),
    Mutation(
        "zip_bomb_ratio_unchecked",
        "src/procurement_agent/security/upload_guard.py",
        "                and info.file_size / max(info.compress_size, 1) > _MAX_COMPRESSION_RATIO",
        "                and False",
        "an archive that inflates a thousand-fold is accepted",
        ("tests/unit/test_upload_guard.py", "tests/unit/test_upload_guard_hostile.py"),
    ),
    Mutation(
        "prod_without_host_list",
        "src/procurement_agent/config.py",
        "        if not self.allowed_hosts:",
        "        if False:",
        "production starts without saying which host names it answers to",
        ("tests/unit/test_network_settings.py", "tests/unit/test_deployment_policy.py"),
    ),
    Mutation(
        "egress_names_unchecked",
        "src/procurement_agent/security/egress.py",
        "            if not guard._is_bind_lookup(host, args, kwargs):\n                guard.check_name(host)\n            results = resolve(",
        "            results = resolve(",
        "a name lookup for any outside host goes through",
        ("tests/unit/test_egress_guard.py", "tests/integration/test_network_policy.py"),
    ),
    Mutation(
        "text_limits_not_applied",
        "src/procurement_agent/ingestion/excel_reader.py",
        '    return bound_text(str(v).replace("\\n", " ").strip())',
        '    return str(v).replace("\\n", " ").strip()',
        "a workbook cell of separators or one huge token reaches the regular expressions",
        ("tests/unit/test_text_limits.py", "tests/integration/test_availability_web.py"),
    ),
]


def run(mutation: Mutation, src_copy: Path, timeout: int) -> tuple[str, float]:
    target = src_copy.parent / mutation.path
    text = target.read_text(encoding="utf-8")
    if text.count(mutation.old) != 1:
        return f"BROKEN ({text.count(mutation.old)} matches of the original text)", 0.0
    target.write_text(text.replace(mutation.old, mutation.new, 1), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(src_copy), str(ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    started = time.monotonic()
    try:
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "--no-header",
             "-m", "not e2e and not docling", *(mutation.suites or SUITES)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
        # pytest exits 1 when tests FAILED; 2-5 mean it did not get to run them (the mutated copy
        # does not import, a path is wrong, nothing was collected) - which proves nothing.
        if done.returncode == 0:
            outcome = "SURVIVED"
        elif done.returncode == 1:
            outcome = "caught"
        else:
            outcome = f"ERROR (pytest exit {done.returncode}: the tests did not run)"
    except subprocess.TimeoutExpired:
        outcome = "caught (hung)"
    finally:
        target.write_text(text, encoding="utf-8")  # put the copy back for the next mutation
    return outcome, time.monotonic() - started


def baseline(src_copy: Path, timeout: int) -> tuple[bool, str]:
    """The unmodified copy must pass every suite any mutation relies on."""
    suites = list(dict.fromkeys([*SUITES, *(s for m in MUTATIONS for s in m.suites)]))
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(src_copy), str(ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header",
         "-m", "not e2e and not docling", *suites],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=False,
    )  # fmt: skip
    summary = [ln for ln in done.stdout.splitlines() if ln.strip()][-1:] or ["(no output)"]
    return done.returncode == 0, summary[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="a mutation name, or a prefix of one")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--no-baseline", action="store_true", help="skip the unmodified-copy run")
    args = ap.parse_args()
    chosen = [m for m in MUTATIONS if not args.only or m.name.startswith(args.only)]
    with tempfile.TemporaryDirectory(prefix="pa-mutate-") as tmp:
        copy = Path(tmp) / "src"
        shutil.copytree(ROOT / "src", copy, ignore=shutil.ignore_patterns("__pycache__"))
        if not args.no_baseline:
            ok, summary = baseline(copy, args.timeout)
            print(
                f"{'baseline ok' if ok else 'BASELINE FAILS'}: the unmodified copy - {summary}",
                flush=True,
            )
            if not ok:
                print(
                    "a mutation is only 'caught' by a suite that passes without it; fix that first"
                )
                return 2
        results: list[tuple[Mutation, str, float]] = []
        for m in chosen:
            outcome, seconds = run(m, copy, args.timeout)
            results.append((m, outcome, seconds))
            print(f"{outcome:9s} {m.name:38s} {seconds:5.0f}s  - {m.why}", flush=True)
    missed = [m.name for m, outcome, _ in results if not outcome.startswith("caught")]
    caught = len(results) - len(missed)
    print(
        f"\n{caught}/{len(results)} mutations caught"
        + (f"; NOT caught: {', '.join(missed)}" if missed else "")
    )
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
