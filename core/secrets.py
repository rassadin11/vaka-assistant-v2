"""Secret provider abstractions for service configuration."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_INFISICAL_URL = "http://localhost:8880"
DEFAULT_INFISICAL_ENV = "dev"
DEFAULT_SECRET_PATH = "/"
_HTTP_TIMEOUT_SECONDS = 10.0


class SecretsProvider(Protocol):
    """Read service configuration secrets by name."""

    def get(self, name: str) -> str:
        """Return a secret value or raise if the secret is unavailable."""


class SecretNotFoundError(KeyError):
    """Raised when a requested secret does not exist."""


class SecretProviderError(RuntimeError):
    """Raised when a secret backend request fails."""


@dataclass(frozen=True, slots=True)
class InfisicalSettings:
    """Infisical Universal Auth settings loaded from environment variables."""

    url: str
    client_id: str
    client_secret: str
    project_id: str
    environment: str


class HttpTransport(Protocol):
    """Minimal synchronous JSON HTTP transport used by the secrets client."""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an HTTP request and return a JSON object response."""


class UrllibHttpTransport:
    """Synchronous JSON transport backed by urllib from the standard library."""

    def __init__(self, *, timeout_seconds: float = _HTTP_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        request_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise SecretProviderError(
                f"Infisical request failed: {exc.code} {exc.reason}: {error_body}"
            ) from exc
        except OSError as exc:
            raise SecretProviderError(f"Infisical request failed: {exc}") from exc

        if not payload:
            return {}
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise SecretProviderError("Infisical response was not a JSON object.")
        return decoded


class EnvSecretsProvider:
    """Secret provider that reads values from process environment variables."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def get(self, name: str) -> str:
        """Return an environment variable value."""

        try:
            return self._environ[name]
        except KeyError as exc:
            raise SecretNotFoundError(name) from exc


class InfisicalSecretsProvider:
    """Secret provider backed by Infisical Universal Auth."""

    def __init__(
        self,
        settings: InfisicalSettings | None = None,
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        self._settings = settings if settings is not None else infisical_settings_from_env()
        self._transport = transport if transport is not None else UrllibHttpTransport()
        self._access_token: str | None = None

    def get(self, name: str) -> str:
        """Fetch a single secret value from Infisical."""

        url = f"{self._settings.url}/api/v4/secrets/{name}?{self._query_params()}"
        payload = self._transport.request_json(
            "GET",
            url,
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        secret = payload.get("secret")
        if not isinstance(secret, dict):
            raise SecretNotFoundError(name)
        value = secret.get("secretValue")
        if not isinstance(value, str):
            raise SecretNotFoundError(name)
        return value

    def list_all(self) -> dict[str, str]:
        """Fetch all shared secrets at the configured secret path."""

        payload = self._transport.request_json(
            "GET",
            f"{self._settings.url}/api/v4/secrets?{self._query_params()}",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        raw_secrets = payload.get("secrets")
        if not isinstance(raw_secrets, list):
            raise SecretProviderError("Infisical secret list response is invalid.")

        secrets: dict[str, str] = {}
        for raw_secret in raw_secrets:
            if not isinstance(raw_secret, dict):
                raise SecretProviderError("Infisical secret list response is invalid.")
            name = raw_secret.get("secretKey")
            value = raw_secret.get("secretValue")
            if not isinstance(name, str) or not name or not isinstance(value, str):
                raise SecretProviderError("Infisical secret list response is invalid.")
            secrets[name] = value
        return secrets

    def _token(self) -> str:
        if self._access_token is not None:
            return self._access_token

        payload = self._transport.request_json(
            "POST",
            f"{self._settings.url}/api/v1/auth/universal-auth/login",
            json_body={
                "clientId": self._settings.client_id,
                "clientSecret": self._settings.client_secret,
            },
        )
        token = payload.get("accessToken")
        if not isinstance(token, str) or not token:
            raise SecretProviderError("Infisical login response did not include an access token.")
        self._access_token = token
        return token

    def _query_params(self) -> str:
        return urlencode(
            {
                "projectId": self._settings.project_id,
                "environment": self._settings.environment,
                "secretPath": DEFAULT_SECRET_PATH,
                "type": "shared",
                "viewSecretValue": "true",
                "expandSecretReferences": "true",
            }
        )


def infisical_settings_from_env() -> InfisicalSettings:
    """Load Infisical settings from environment variables."""

    return InfisicalSettings(
        url=os.getenv("INFISICAL_URL", DEFAULT_INFISICAL_URL).rstrip("/"),
        client_id=_required_env("INFISICAL_CLIENT_ID"),
        client_secret=_required_env("INFISICAL_CLIENT_SECRET"),
        project_id=_required_env("INFISICAL_PROJECT_ID"),
        environment=os.getenv("INFISICAL_ENV", DEFAULT_INFISICAL_ENV),
    )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise SecretProviderError(f"{name} is required.")
    return value
