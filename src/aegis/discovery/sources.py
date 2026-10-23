"""Read-only metadata sources. Nothing here downloads, installs, or executes third-party code."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from aegis.discovery.models import Candidate, CandidateKind, Provenance

ALLOWED_HOSTS = frozenset({"registry.modelcontextprotocol.io", "pypi.org"})
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 20

# (url) -> raw bytes. Injected so tests can run offline from recorded fixtures.
Fetcher = Callable[[str], bytes]


class DiscoveryError(Exception):
    pass


def https_fetch(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise DiscoveryError(f"host not allowlisted: {url}")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "aegis-discovery/0.1"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - scheme/host checked above
        data: bytes = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise DiscoveryError("response too large")
    return data


def _provenance(url: str, raw: bytes) -> Provenance:
    return Provenance(source=url, fetched_at=datetime.now(UTC), response_sha256=hashlib.sha256(raw).hexdigest())


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class Source(Protocol):
    name: str

    def search(self, query: str, limit: int = 10) -> list[Candidate]: ...


class McpRegistrySource:
    """Official MCP registry: GET /v0/servers?search=&limit= (verified live 2026-10-23)."""

    name = "mcp_registry"
    BASE = "https://registry.modelcontextprotocol.io/v0/servers"

    def __init__(self, fetch: Fetcher = https_fetch) -> None:
        self._fetch = fetch

    def search(self, query: str, limit: int = 10) -> list[Candidate]:
        url = f"{self.BASE}?{urllib.parse.urlencode({'search': query, 'limit': limit})}"
        raw = self._fetch(url)
        prov = _provenance(url, raw)
        data: dict[str, Any] = json.loads(raw)
        latest: dict[str, Candidate] = {}
        for entry in data.get("servers", []):
            server = entry.get("server", {})
            meta = entry.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})
            if meta.get("status", "active") != "active":
                continue
            repo = server.get("repository") or {}
            remotes = server.get("remotes") or []
            packages = server.get("packages") or []
            transports = sorted(
                {r.get("type", "") for r in remotes}
                | {(p.get("transport") or {}).get("type", "") for p in packages} - {""}
            )
            cand = Candidate(
                kind=CandidateKind.MCP_SERVER,
                name=server.get("name", "?"),
                version=server.get("version"),
                description=server.get("description", ""),
                repository_url=repo.get("url"),
                homepage=server.get("websiteUrl"),
                last_updated=_parse_dt(meta.get("updatedAt")),
                transports=[t for t in transports if t],
                is_remote=bool(remotes) and not packages,
                provenance=prov,
            )
            # Registry returns one row per version; keep the latest-flagged or most recent.
            prev = latest.get(cand.name)
            if (
                meta.get("isLatest")
                or prev is None
                or (cand.last_updated or prov.fetched_at) > (prev.last_updated or prov.fetched_at)
            ):
                latest[cand.name] = cand
        return list(latest.values())


class PyPISource:
    """PyPI JSON API for *named* packages (PyPI has no search API). Metadata only."""

    name = "pypi"
    BASE = "https://pypi.org/pypi"

    def __init__(self, fetch: Fetcher = https_fetch) -> None:
        self._fetch = fetch

    def search(self, query: str, limit: int = 10) -> list[Candidate]:
        names = [n.strip() for n in query.split(",") if n.strip()][:limit]
        return [c for n in names if (c := self.lookup(n)) is not None]

    def lookup(self, package: str) -> Candidate | None:
        url = f"{self.BASE}/{urllib.parse.quote(package)}/json"
        try:
            raw = self._fetch(url)
        except Exception:  # noqa: BLE001 - 404 / network errors => no candidate
            return None
        info = json.loads(raw)
        meta = info.get("info", {})
        files = info.get("urls", [])
        uploaded = [_parse_dt(f.get("upload_time_iso_8601")) for f in files]
        urls = meta.get("project_urls") or {}
        repo = next(
            (v for k, v in urls.items() if any(m in k.lower() for m in ("source", "repo", "code", "github"))), None
        )
        lic = meta.get("license_expression") or meta.get("license") or None
        if lic and len(lic) > 60:  # some packages paste the full licence text
            lic = lic.splitlines()[0][:60]
        return Candidate(
            kind=CandidateKind.PYPI_PACKAGE,
            name=meta.get("name", package),
            version=meta.get("version"),
            description=meta.get("summary") or "",
            repository_url=repo,
            homepage=meta.get("home_page") or urls.get("Homepage"),
            license=lic,
            last_updated=max((u for u in uploaded if u), default=None),
            requires_python=meta.get("requires_python"),
            platforms=sorted(
                {
                    f["filename"].rsplit("-", 1)[-1].removesuffix(".whl")
                    for f in files
                    if f.get("packagetype") == "bdist_wheel"
                }
            ),
            provenance=_provenance(url, raw),
        )
