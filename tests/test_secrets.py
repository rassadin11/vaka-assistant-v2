from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from core.secrets import (
    EnvSecretsProvider,
    InfisicalSecretsProvider,
    InfisicalSettings,
    SecretNotFoundError,
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Mapping[str, str] | None, Mapping[str, Any] | None]] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, url, headers, json_body))
        if url.endswith("/api/v1/auth/universal-auth/login"):
            return {"accessToken": "test-access-token"}
        return {"secret": {"secretKey": "OPENROUTER_API_KEY", "secretValue": "test-secret"}}


def test_env_secrets_provider_reads_value() -> None:
    provider = EnvSecretsProvider({"OPENROUTER_API_KEY": "from-env"})

    assert provider.get("OPENROUTER_API_KEY") == "from-env"


def test_env_secrets_provider_raises_for_missing_value() -> None:
    provider = EnvSecretsProvider({})

    with pytest.raises(SecretNotFoundError):
        provider.get("OPENROUTER_API_KEY")


def test_infisical_secrets_provider_logs_in_and_fetches_secret() -> None:
    transport = FakeTransport()
    provider = InfisicalSecretsProvider(
        InfisicalSettings(
            url="http://infisical.local",
            client_id="client-id",
            client_secret="client-secret",
            project_id="project-id",
            environment="dev",
        ),
        transport=transport,
    )

    assert provider.get("OPENROUTER_API_KEY") == "test-secret"
    assert len(transport.calls) == 2
    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][3] == {"clientId": "client-id", "clientSecret": "client-secret"}
    assert transport.calls[1][0] == "GET"
    assert "projectId=project-id" in transport.calls[1][1]
    assert transport.calls[1][2] == {"Authorization": "Bearer test-access-token"}


def test_infisical_secrets_provider_reuses_access_token() -> None:
    transport = FakeTransport()
    provider = InfisicalSecretsProvider(
        InfisicalSettings(
            url="http://infisical.local",
            client_id="client-id",
            client_secret="client-secret",
            project_id="project-id",
            environment="dev",
        ),
        transport=transport,
    )

    assert provider.get("FIRST") == "test-secret"
    assert provider.get("SECOND") == "test-secret"
    login_calls = [
        call for call in transport.calls if call[1].endswith("/api/v1/auth/universal-auth/login")
    ]
    assert len(login_calls) == 1
