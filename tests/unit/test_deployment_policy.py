"""The deployment's security policy, asserted against the files that define it.

Nothing here needs Docker: the compose files and the Dockerfile are parsed and the properties that
make "nothing leaves this computer" true in a container are checked - no route out, no more
connectivity than each pair of services needs, no capabilities, nothing but loopback published,
a model server that cannot forward to a hosted service. Each of them was checked against a running
stack once (see docs/SECURITY.md); this keeps them from drifting.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from procurement_agent.config import check_llm_url

ROOT = Path(__file__).resolve().parents[2]


def _compose(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


PROD = _compose("docker-compose.yml")
DEV = _compose("docker-compose.dev.yml")
BOTH = [pytest.param(PROD, id="prod"), pytest.param(DEV, id="dev")]


def _networks(service: dict[str, Any]) -> set[str]:
    nets = service.get("networks", [])
    return set(nets) if isinstance(nets, list | dict) else set()


def _env(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment", {})
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _service(doc: dict[str, Any], *names: str) -> dict[str, Any]:
    for name in names:
        if name in doc["services"]:
            return doc["services"][name]  # type: ignore[no-any-return]
    raise AssertionError(f"none of {names} in {list(doc['services'])}")


# --- the project name ------------------------------------------------------------------------


def test_the_compose_project_has_a_fixed_name_so_the_volumes_do_not_depend_on_the_folder() -> None:
    """Without a top-level ``name`` Compose names the project after the directory it is started
    from, and the volumes ``app-data`` (documents, API keys, audit trail) and ``ollama`` (the
    models) after the project. A release unpacked into a new folder would then start with empty
    volumes and no error. ``-p`` and ``COMPOSE_PROJECT_NAME`` still override it on purpose."""
    name = PROD.get("name")
    assert name == "procurement-agent"
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name)
    assert DEV["name"] != name, "the development stack must never share volumes with this one"
    for volume in PROD["volumes"].values():
        assert not volume or "name" not in volume, (
            "a volume pinned by name would escape the project"
        )


# --- what a container may do -----------------------------------------------------------------


@pytest.mark.parametrize("doc", BOTH)
def test_every_service_drops_every_capability_and_cannot_regain_privilege(
    doc: dict[str, Any],
) -> None:
    for name, svc in doc["services"].items():
        assert svc.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in svc.get("security_opt", []), name
        extra = set(svc.get("cap_add", []))
        # the Caddy binary carries a file capability and will not exec without it in the set
        assert extra <= {"NET_BIND_SERVICE"}, (name, extra)
        if name in ("app", "ollama"):
            assert not extra, name


@pytest.mark.parametrize("doc", BOTH)
def test_every_service_is_bounded_and_leaves_no_core_dump(doc: dict[str, Any]) -> None:
    for name, svc in doc["services"].items():
        assert svc.get("mem_limit"), name
        assert svc.get("pids_limit"), name
        assert svc.get("ulimits", {}).get("core") == 0, name  # a crash must not write document text


def test_every_production_service_has_a_cpu_setting() -> None:
    for name, svc in PROD["services"].items():
        assert "cpus" in svc, name


@pytest.mark.parametrize("doc", BOTH)
def test_root_filesystems_are_read_only_and_tmpfs_is_noexec(doc: dict[str, Any]) -> None:
    for name, svc in doc["services"].items():
        assert svc.get("read_only") is True, name
        for mount in svc.get("tmpfs", []):
            flags = set(mount.split(":", 1)[1].split(","))
            assert {"noexec", "nosuid", "nodev"} <= flags, (name, mount)


@pytest.mark.parametrize("doc", BOTH)
def test_nothing_reaches_the_host_beyond_what_is_needed(doc: dict[str, Any]) -> None:
    forbidden = (
        "privileged",
        "network_mode",
        "pid",
        "ipc",
        "userns_mode",
        "devices",
        "uts",
        "cgroup",
    )
    for name, svc in doc["services"].items():
        for key in forbidden:
            assert key not in svc, (name, key)
        for volume in svc.get("volumes", []):
            source = volume.split(":", 1)[0]
            assert "docker.sock" not in volume and not source.startswith("/"), (name, volume)
            # the only host path is the model directory, and it is read-only
            if source.startswith("."):
                assert source == "./models" and volume.endswith(":ro"), (name, volume)


@pytest.mark.parametrize("doc", BOTH)
def test_only_loopback_is_published(doc: dict[str, Any]) -> None:
    published = [(n, p) for n, s in doc["services"].items() for p in s.get("ports", [])]
    assert published, "the relay publishes the one port"
    for name, port in published:
        assert str(port).startswith("127.0.0.1:"), (name, port)
    assert not any(s.get("ports") for n, s in doc["services"].items() if n in ("app", "ollama"))


# --- the networks ----------------------------------------------------------------------------


def test_every_network_a_backend_uses_is_internal() -> None:
    """``internal: true`` is what removes the route out; ``edge`` is the relay's side only."""
    for doc in (PROD, DEV):
        for name in ("app", "ollama"):
            if name not in doc["services"]:
                continue
            for net in _networks(doc["services"][name]):
                assert doc["networks"][net].get("internal") is True, (name, net)
    users_of_edge = [n for n, s in PROD["services"].items() if "edge" in _networks(s)]
    assert users_of_edge == ["proxy"]


def test_each_pair_of_services_shares_only_the_network_it_needs() -> None:
    s = PROD["services"]
    app, ollama, proxy = _networks(s["app"]), _networks(s["ollama"]), _networks(s["proxy"])
    assert app & proxy == {"internal"}
    assert app & ollama == {"llm"}
    assert proxy & ollama == set(), "the relay must not be able to reach the model server"
    assert ollama == {"llm"}


def test_the_dev_model_server_is_on_an_internal_network_behind_a_loopback_relay() -> None:
    ollama = DEV["services"]["ollama"]
    relay = DEV["services"]["relay"]
    assert not ollama.get("ports")
    assert _networks(ollama) == {"llm"}
    assert DEV["networks"]["llm"]["internal"] is True
    assert relay["ports"] == ["127.0.0.1:11434:11434"]
    # masquerade off: the relay's own outbound traffic gets no NAT
    edge = DEV["networks"]["edge"]["driver_opts"]
    assert edge["com.docker.network.bridge.enable_ip_masquerade"] == "false"


# --- the model server ------------------------------------------------------------------------


@pytest.mark.parametrize("doc", BOTH)
def test_the_model_server_cannot_forward_to_a_hosted_service(doc: dict[str, Any]) -> None:
    assert _env(doc["services"]["ollama"]).get("OLLAMA_NO_CLOUD") == "1"


def test_images_are_pinned_by_digest_and_both_compose_files_run_the_same_ollama() -> None:
    for doc in (PROD, DEV):
        for name, svc in doc["services"].items():
            if "build" not in svc:
                assert re.search(r"@sha256:[0-9a-f]{64}$", svc["image"]), (name, svc["image"])
    assert PROD["services"]["ollama"]["image"] == DEV["services"]["ollama"]["image"]
    script = (ROOT / "scripts" / "switch_dev_ollama.sh").read_text(encoding="utf-8")
    assert PROD["services"]["ollama"]["image"] in script, "the rollback must start the pinned image"


# --- the application service ---------------------------------------------------------------------


def test_the_app_is_production_and_cannot_start_without_its_host_list() -> None:
    env = _env(PROD["services"]["app"])
    assert env["PA_ENV"] == "prod"
    assert env["PA_ALLOWED_HOSTS"].startswith(
        "${PA_ALLOWED_HOSTS:?"
    )  # compose refuses an empty one
    assert "PA_DEV_API_KEY" not in env
    assert env.get("PA_EGRESS_GUARD", "true").lower() not in ("0", "false", "no", "off")


def test_the_app_talks_to_the_model_server_by_the_compose_service_name_only() -> None:
    url = _env(PROD["services"]["app"])["PA_OLLAMA_BASE_URL"]
    try:
        accepted = check_llm_url(url)  # strict policy: this machine or the compose service
    except ValueError as refused:
        pytest.fail(str(refused))
    assert accepted == url


def test_only_the_relay_may_set_the_client_address() -> None:
    forwarded = _env(PROD["services"]["app"])["FORWARDED_ALLOW_IPS"]
    assert "PA_PROXY_IP" in forwarded
    assert "*" not in forwarded


def test_the_relay_has_header_body_and_idle_timeouts() -> None:
    caddyfile = _env(PROD["services"]["proxy"])["CADDYFILE"]
    assert re.search(r"^\s*admin off\s*$", caddyfile, re.M)
    for directive in ("read_header", "read_body", "idle"):
        assert re.search(rf"^\s*{directive} \d+[sm]\s*$", caddyfile, re.M), directive
    # a write timeout would cut off a report that takes minutes to compute
    assert not re.search(r"^\s*write \d", caddyfile, re.M)


# --- the image -----------------------------------------------------------------------------------


DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_the_image_runs_as_a_non_root_user() -> None:
    users = re.findall(r"^USER (\S+)", DOCKERFILE, re.M)
    assert users and users[-1] == "app"
    assert DOCKERFILE.rindex("USER app") < DOCKERFILE.rindex("CMD [")


def test_the_image_is_offline_by_default_and_trusts_one_proxy_hop_at_most() -> None:
    for pair in (
        "HF_HUB_OFFLINE=1",
        "TRANSFORMERS_OFFLINE=1",
        "HF_HUB_DISABLE_TELEMETRY=1",
        "DO_NOT_TRACK=1",
    ):
        assert pair in DOCKERFILE, pair
    assert "FORWARDED_ALLOW_IPS=127.0.0.1" in DOCKERFILE
    assert "--proxy-headers" in DOCKERFILE and "--workers" in DOCKERFILE


def test_a_downloaded_build_input_is_pinned_by_checksum() -> None:
    for line in DOCKERFILE.splitlines():
        if re.match(r"ADD\b", line) and "http" in line:
            assert "--checksum=sha256:" in line, line
    assert not re.search(r"curl[^\n]*\|\s*(ba)?sh", DOCKERFILE)
    assert "sudo" not in DOCKERFILE
    assert ":latest" not in DOCKERFILE


def test_the_build_context_is_an_allow_list() -> None:
    lines = [
        ln.strip()
        for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert lines[0] == "*"
    included = {ln[1:] for ln in lines if ln.startswith("!")}
    # scripts/supervise.py: the stdlib-only process that restarts the server (docker-compose.yml)
    assert included <= {"pyproject.toml", "uv.lock", "README.md", "src/", "scripts/supervise.py"}
