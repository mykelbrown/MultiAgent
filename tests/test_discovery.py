from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aegis.core.audit import AuditLog
from aegis.core.permissions import ApprovalStore, Capability, PermissionDenied, PermissionGate
from aegis.discovery.models import ToolStatus
from aegis.discovery.registry import DiscoveryService, ToolRegistry
from aegis.discovery.sources import DiscoveryError, McpRegistrySource, PyPISource, https_fetch

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 23, tzinfo=UTC)


def fixture_fetch(url: str) -> bytes:
    if "modelcontextprotocol" in url:
        return (FIX / "mcp_registry_metatrader.json").read_bytes()
    if url.endswith("/MetaTrader5/json"):
        return (FIX / "pypi_metatrader5.json").read_bytes()
    if url.endswith("/pydantic/json"):
        return (FIX / "pypi_pydantic.json").read_bytes()
    raise DiscoveryError("404")


def _service(tmp_path):
    audit = AuditLog()
    approvals = ApprovalStore(audit=audit)
    registry = ToolRegistry(tmp_path / "registry.json", audit, approvals)
    svc = DiscoveryService(
        [McpRegistrySource(fixture_fetch), PyPISource(fixture_fetch)], registry, PermissionGate(audit, approvals), audit
    )
    return svc, registry, approvals, audit


def test_fetch_refuses_non_allowlisted_hosts():
    for url in ("https://evil.example.com/x", "http://pypi.org/pypi/x/json", "file:///etc/passwd"):
        with pytest.raises(DiscoveryError):
            https_fetch(url)


def test_mcp_registry_dedupes_versions_and_flags_remote():
    cands = McpRegistrySource(fixture_fetch).search("metatrader")
    names = sorted(c.name for c in cands)
    assert names == ["com.telegramtometatrader/ttmt", "io.tickwire/metatrader"]
    ttmt = next(c for c in cands if c.name.startswith("com.telegram"))
    assert ttmt.version == "1.0.1" and ttmt.is_remote and ttmt.provenance.response_sha256


def test_pypi_metadata_and_platforms():
    mt5 = PyPISource(fixture_fetch).lookup("MetaTrader5")
    assert mt5 is not None and mt5.version == "5.0.6231"
    assert all(p.endswith("win_amd64") for p in mt5.platforms)
    assert PyPISource(fixture_fetch).lookup("does-not-exist") is None


def test_scoring_flags_and_platform_compat(tmp_path):
    svc, *_ = _service(tmp_path)
    results = {s.candidate.name: s for s in svc.discover("MetaTrader5,pydantic", platform="manylinux_2_17_x86_64")}
    mt5 = results["metatrader5"].score  # PyPI returns the normalized name
    assert mt5.compatibility < 1 and any("no wheel" in f for f in mt5.flags)
    pyd = results["pydantic"].score
    assert pyd.license == 5.0 and pyd.compatibility == 5.0  # pure-python wheel
    remote = [s for s in results.values() if s.candidate.is_remote]
    assert remote and all(any("remote hosted" in f for f in s.score.flags) for s in remote)


def test_discovery_never_auto_approves(tmp_path):
    svc, registry, *_ = _service(tmp_path)
    svc.discover("metatrader")
    assert registry.entries() and all(e.status is ToolStatus.DISCOVERED for e in registry.entries())


def test_lifecycle_requires_scoped_approval_and_supports_rollback(tmp_path):
    svc, registry, approvals, audit = _service(tmp_path)
    svc.discover("pydantic")
    key = "pypi_package:pydantic"
    with pytest.raises(ValueError):
        registry.transition(key, ToolStatus.APPROVED, "owner")  # must quarantine first
    registry.transition(key, ToolStatus.QUARANTINED, "owner")
    approvals.grant(Capability.INSTALL_TOOL, "owner", scope={"tool": "pypi_package:something-else"})
    with pytest.raises(PermissionDenied):
        registry.transition(key, ToolStatus.APPROVED, "owner")
    approvals.grant(Capability.INSTALL_TOOL, "owner", scope={"tool": key})
    entry = registry.transition(key, ToolStatus.APPROVED, "owner")
    assert entry.pinned_version == "2.14.1"  # exact version from recorded metadata
    entry = registry.transition(key, ToolStatus.ROLLED_BACK, "owner")
    assert entry.status is ToolStatus.ROLLED_BACK and entry.pinned_version is None
    # Persisted with history.
    reloaded = ToolRegistry(tmp_path / "registry.json", audit, approvals).get(key)
    assert reloaded is not None and [h["status"] for h in reloaded.history] == [
        "quarantined",
        "approved",
        "rolled_back",
    ]


def test_source_failure_is_isolated_and_audited(tmp_path):
    def broken(url: str) -> bytes:
        raise DiscoveryError("boom")

    audit = AuditLog()
    approvals = ApprovalStore(audit=audit)
    svc = DiscoveryService(
        [McpRegistrySource(broken), PyPISource(fixture_fetch)],
        ToolRegistry(None, audit, approvals),
        PermissionGate(audit, approvals),
        audit,
    )
    assert [s.candidate.name for s in svc.discover("pydantic")] == ["pydantic"]
    assert audit.records("discovery.source_error")


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("AEGIS_NETWORK_TESTS") != "1", reason="set AEGIS_NETWORK_TESTS=1")
def test_live_read_only_discovery():
    """Harmless integration test: real HTTPS GETs to the MCP registry and PyPI, metadata only."""
    assert McpRegistrySource().search("filesystem", limit=3)
    pkg = PyPISource().lookup("pydantic")
    assert pkg is not None and pkg.version


def test_source_repo_detected_from_varied_project_url_keys():
    import json

    raw = json.dumps(
        {"info": {"name": "x", "version": "1.0", "project_urls": {"Source Code": "https://github.com/a/x"}}, "urls": []}
    ).encode()
    cand = PyPISource(lambda url: raw).lookup("x")
    assert cand is not None and cand.repository_url == "https://github.com/a/x"
