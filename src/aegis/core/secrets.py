"""Secret lookup: OS keyring first (if the optional `keyring` package is installed), then env vars.

Secrets are returned to the caller only; they are never logged, written to audit payloads,
or placed in prompts. The repository never contains secret values.
"""

from __future__ import annotations

import os

KEYRING_SERVICE = "aegis-ai"


class MissingSecret(Exception):
    pass


def get_secret(name: str) -> str:
    try:
        import keyring  # type: ignore[import-not-found,unused-ignore]

        value = keyring.get_password(KEYRING_SERVICE, name)
        if value:
            return str(value)
    except Exception:  # noqa: BLE001,S110 - keyring absent or backend unavailable: fall back to env
        pass
    value = os.environ.get(name)
    if not value:
        raise MissingSecret(f"secret {name} not set (Windows Credential Manager service '{KEYRING_SERVICE}' or env)")
    return value
