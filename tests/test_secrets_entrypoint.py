from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from core import secrets_entrypoint
from core.secrets import InfisicalSecretsProvider, InfisicalSettings, SecretProviderError


class FakeTransport:
    def __init__(self, *, login_error: bool = False) -> None:
        self.login_error = login_error
        self.calls: list[tuple[str, str]] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del headers, json_body
        self.calls.append((method, url))
        if url.endswith("/api/v1/auth/universal-auth/login"):
            if self.login_error:
                raise SecretProviderError("login failed")
            return {"accessToken": "test-token"}
        return {
            "secrets": [
                {"secretKey": "FROM_INFISICAL", "secretValue": "loaded-value"},
                {"secretKey": "TOPOLOGY", "secretValue": "should-not-win"},
            ]
        }


def fake_provider(*, login_error: bool = False) -> InfisicalSecretsProvider:
    return InfisicalSecretsProvider(
        InfisicalSettings(
            url="http://infisical.test",
            client_id="client-id",
            client_secret="client-secret",
            project_id="project-id",
            environment="prod",
        ),
        transport=FakeTransport(login_error=login_error),
    )


def test_run_merges_secrets_without_overriding_existing_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environ = {"TOPOLOGY": "compose-value"}
    exec_calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        secrets_entrypoint.os,
        "execvp",
        lambda executable, arguments: exec_calls.append((executable, arguments)),
    )

    result = secrets_entrypoint.run(
        ["python", "-m", "worker"], provider=fake_provider(), environ=environ
    )

    assert result == 0
    assert environ == {"TOPOLOGY": "compose-value", "FROM_INFISICAL": "loaded-value"}
    assert exec_calls == [("python", ["python", "-m", "worker"])]


def test_run_fails_closed_when_infisical_authentication_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        secrets_entrypoint.os,
        "execvp",
        lambda executable, arguments: pytest.fail("execvp must not be called"),
    )

    result = secrets_entrypoint.run(["python"], provider=fake_provider(login_error=True))

    assert result == 1
    assert capsys.readouterr().err.startswith("Infisical secret loading failed: ")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["--", "python", "-m", "gateway", "serve"], ["python", "-m", "gateway", "serve"])],
)
def test_parse_command_accepts_command_after_separator(
    argv: list[str], expected: list[str]
) -> None:
    assert secrets_entrypoint.parse_command(argv) == expected


@pytest.mark.parametrize("argv", [[], ["python"], ["--"]])
def test_parse_command_requires_separator_and_command(argv: list[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        secrets_entrypoint.parse_command(argv)
