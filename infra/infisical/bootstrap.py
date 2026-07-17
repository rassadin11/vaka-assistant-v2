"""Bootstrap a local development Infisical instance.

This dev-safe script defaults to the existing local development topology. Its
URL, organization, project, environment, and input/output paths can be
overridden through ``BOOTSTRAP_*`` environment variables for a separate run.
It writes credentials only to the configured out-of-repository file and masks
sensitive values in stdout.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.crypto import generate_base64_kek

INFISICAL_URL = os.getenv("INFISICAL_URL", "http://localhost:8880").rstrip("/")
ADMIN_EMAIL = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@dev.local")
ORG_NAME = os.getenv("BOOTSTRAP_ORG_NAME", "Personal Assistant Dev")
PROJECT_NAME = os.getenv("BOOTSTRAP_PROJECT_NAME", "personal-assistant")
PROJECT_SLUG = os.getenv("BOOTSTRAP_PROJECT_SLUG", "personal-assistant")
ENV_NAME = os.getenv("BOOTSTRAP_ENV_NAME", "Development")
ENV_SLUG = os.getenv("BOOTSTRAP_ENV_SLUG", "dev")
SECRET_PATH = "/"
SERVICE_NAMES = ("gateway", "worker", "scheduler", "webapp")
INPUT_ENV_PATH = Path(
    os.getenv("BOOTSTRAP_INPUT_ENV_PATH", r"C:\Users\Artem\.assistant\bootstrap.env")
)
OUTPUT_ENV_PATH = Path(
    os.getenv("BOOTSTRAP_OUTPUT_ENV_PATH", r"C:\Users\Artem\.assistant\infisical-dev.env")
)
SEED_KEYS = tuple(
    key.strip()
    for key in os.getenv(
        "BOOTSTRAP_SEED_KEYS",
        "TELEGRAM_BOT_TOKEN,TELEGRAM_BOT_TOKEN_PROD,TELEGRAM_BOT_TOKEN_TEST,"
        "OPENROUTER_API_KEY,WEBAPP_SESSION_SECRET",
    ).split(",")
    if key.strip()
)
HTTP_TIMEOUT_SECONDS = 20.0


class InfisicalBootstrapError(RuntimeError):
    """Raised when local Infisical bootstrap fails."""


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    name: str
    identity_id: str
    client_id: str
    client_secret: str


@dataclass(frozen=True, slots=True)
class BootstrapState:
    admin_email: str
    admin_password: str
    admin_token: str
    org_id: str
    org_slug: str
    project_id: str
    project_slug: str
    services: dict[str, ServiceIdentity]


def main() -> int:
    existing_env = read_env_file(OUTPUT_ENV_PATH)
    admin_password = existing_env.get("INFISICAL_ADMIN_PASSWORD") or generate_password()

    existing_admin_token = existing_env.get("INFISICAL_ADMIN_TOKEN")
    bootstrap_payload = bootstrap_instance(admin_password, existing_admin_token)
    admin_token = existing_admin_token or extract_admin_token(bootstrap_payload)

    org_id, org_slug = organization_from_payload_or_api(
        bootstrap_payload,
        admin_token,
        existing_env,
    )
    project = get_or_create_project(admin_token)
    project_id = require_str(project, "id")
    project_slug = require_str(project, "slug")
    ensure_environment(admin_token, project)

    services: dict[str, ServiceIdentity] = {}
    for service_name in SERVICE_NAMES:
        services[service_name] = ensure_service_identity(
            admin_token,
            org_id,
            project_id,
            service_name,
            existing_env,
        )

    seed_secrets(admin_token, project_id)

    state = BootstrapState(
        admin_email=ADMIN_EMAIL,
        admin_password=admin_password,
        admin_token=admin_token,
        org_id=org_id,
        org_slug=org_slug,
        project_id=project_id,
        project_slug=project_slug,
        services=services,
    )
    write_output_env(state)
    verification = verify_service_access(state)
    print_summary(state, verification)
    return 0 if all(verification.values()) else 1


def bootstrap_instance(admin_password: str, existing_admin_token: str | None) -> dict[str, Any]:
    if existing_admin_token:
        return {}

    try:
        return request_json(
            "POST",
            "/api/v1/admin/bootstrap",
            json_body={
                "email": ADMIN_EMAIL,
                "password": admin_password,
                "organization": ORG_NAME,
            },
        )
    except InfisicalBootstrapError as exc:
        config = request_json("GET", "/api/v1/admin/config")
        config_data = config.get("config")
        if isinstance(config_data, dict) and config_data.get("initialized") is True:
            raise InfisicalBootstrapError(
                f"Infisical is already initialized and {OUTPUT_ENV_PATH} has no admin token. "
                "Re-run with a valid existing INFISICAL_ADMIN_TOKEN in that file."
            ) from exc
        raise


def organization_from_payload_or_api(
    payload: Mapping[str, Any],
    admin_token: str,
    existing_env: Mapping[str, str],
) -> tuple[str, str]:
    organization = payload.get("organization")
    if isinstance(organization, dict):
        return require_str(organization, "id"), require_str(organization, "slug")

    org_id = existing_env.get("INFISICAL_ORG_ID", "")
    org_slug = existing_env.get("INFISICAL_ORG_SLUG", "")
    if org_id and org_slug:
        return org_id, org_slug

    organization = find_organization(admin_token)
    return require_str(organization, "id"), require_str(organization, "slug")


def find_organization(admin_token: str) -> dict[str, Any]:
    payload = request_json(
        "GET",
        "/api/v1/admin/organization-management/organizations?limit=100&offset=0",
        token=admin_token,
    )
    organizations = payload.get("organizations")
    if not isinstance(organizations, list):
        raise InfisicalBootstrapError("Organization list response is invalid.")
    for organization in organizations:
        if isinstance(organization, dict) and organization.get("name") == ORG_NAME:
            return organization
    if organizations and isinstance(organizations[0], dict):
        return organizations[0]
    raise InfisicalBootstrapError("No organization found after bootstrap.")


def get_or_create_project(admin_token: str) -> dict[str, Any]:
    projects_payload = request_json(
        "GET",
        "/api/v1/projects?" + urlencode({"type": "secret-manager", "includeRoles": "false"}),
        token=admin_token,
    )
    projects = projects_payload.get("projects")
    if not isinstance(projects, list):
        raise InfisicalBootstrapError("Project list response is invalid.")
    for project in projects:
        if isinstance(project, dict) and project.get("slug") == PROJECT_SLUG:
            return project

    payload = request_json(
        "POST",
        "/api/v1/projects",
        token=admin_token,
        json_body={
            "projectName": PROJECT_NAME,
            "projectDescription": "Local development secrets for personal-assistant.",
            "slug": PROJECT_SLUG,
            "type": "secret-manager",
            "shouldCreateDefaultEnvs": False,
            "hasDeleteProtection": False,
        },
    )
    project = payload.get("project")
    if not isinstance(project, dict):
        raise InfisicalBootstrapError("Project create response is invalid.")
    return project


def ensure_environment(admin_token: str, project: Mapping[str, Any]) -> None:
    project_id = require_str(project, "id")
    environments = project.get("environments")
    if isinstance(environments, list):
        for environment in environments:
            if isinstance(environment, dict) and environment.get("slug") == ENV_SLUG:
                return

    try:
        request_json(
            "POST",
            f"/api/v1/projects/{project_id}/environments",
            token=admin_token,
            json_body={"name": ENV_NAME, "slug": ENV_SLUG, "position": 1},
        )
    except InfisicalBootstrapError as exc:
        if "already exists" not in str(exc).lower():
            raise


def ensure_service_identity(
    admin_token: str,
    org_id: str,
    project_id: str,
    service_name: str,
    existing_env: Mapping[str, str],
) -> ServiceIdentity:
    env_prefix = env_key_prefix(service_name)
    existing_client_id = existing_env.get(f"{env_prefix}_INFISICAL_CLIENT_ID")
    identity = find_identity(admin_token, org_id, service_name, existing_client_id)
    if identity is None:
        payload = request_json(
            "POST",
            "/api/v1/identities",
            token=admin_token,
            json_body={
                "name": service_name,
                "organizationId": org_id,
                "role": "no-access",
                "hasDeleteProtection": False,
            },
        )
        identity = payload.get("identity")
        if not isinstance(identity, dict):
            raise InfisicalBootstrapError(
                f"Identity create response for {service_name} is invalid."
            )

    identity_id = require_str(identity, "id")
    attach_universal_auth(admin_token, identity_id)
    ensure_project_membership(admin_token, project_id, identity_id)

    client_id = existing_client_id or get_universal_auth_client_id(admin_token, identity_id)
    client_secret = existing_env.get(f"{env_prefix}_INFISICAL_CLIENT_SECRET")
    if not client_secret:
        client_secret = create_client_secret(admin_token, identity_id, service_name)

    return ServiceIdentity(
        name=service_name,
        identity_id=identity_id,
        client_id=client_id,
        client_secret=client_secret,
    )


def find_identity(
    admin_token: str,
    org_id: str,
    service_name: str,
    expected_client_id: str | None,
) -> dict[str, Any] | None:
    payload = request_json(
        "GET",
        "/api/v1/identities?" + urlencode({"orgId": org_id}),
        token=admin_token,
    )
    identities = payload.get("identities")
    if not isinstance(identities, list):
        raise InfisicalBootstrapError("Identity list response is invalid.")
    candidates: list[dict[str, Any]] = []
    for identity in identities:
        if not isinstance(identity, dict):
            continue
        nested_identity = identity.get("identity")
        if isinstance(nested_identity, dict) and nested_identity.get("name") == service_name:
            candidates.append(nested_identity)
        elif identity.get("name") == service_name:
            candidates.append(identity)
    if not candidates:
        return None
    if expected_client_id:
        for candidate in candidates:
            candidate_id = require_str(candidate, "id")
            try:
                client_id = get_universal_auth_client_id(admin_token, candidate_id)
            except InfisicalBootstrapError:
                continue
            if client_id == expected_client_id:
                return candidate
    return candidates[0]


def attach_universal_auth(admin_token: str, identity_id: str) -> None:
    try:
        request_json(
            "POST",
            f"/api/v1/auth/universal-auth/identities/{identity_id}",
            token=admin_token,
            json_body={
                "clientSecretTrustedIps": [{"ipAddress": "0.0.0.0/0"}, {"ipAddress": "::/0"}],
                "accessTokenTrustedIps": [{"ipAddress": "0.0.0.0/0"}, {"ipAddress": "::/0"}],
                "accessTokenTTL": 2592000,
                "accessTokenMaxTTL": 2592000,
                "accessTokenNumUsesLimit": 0,
                "accessTokenPeriod": 0,
                "lockoutEnabled": True,
                "lockoutThreshold": 3,
                "lockoutDurationSeconds": 300,
                "lockoutCounterResetSeconds": 30,
            },
        )
    except InfisicalBootstrapError as exc:
        if "already" not in str(exc).lower():
            raise


def get_universal_auth_client_id(admin_token: str, identity_id: str) -> str:
    payload = request_json(
        "GET",
        f"/api/v1/auth/universal-auth/identities/{identity_id}",
        token=admin_token,
    )
    universal_auth = payload.get("identityUniversalAuth")
    if not isinstance(universal_auth, dict):
        raise InfisicalBootstrapError("Universal Auth config response is invalid.")
    return require_str(universal_auth, "clientId")


def ensure_project_membership(admin_token: str, project_id: str, identity_id: str) -> None:
    body = {"roles": [{"role": "viewer", "isTemporary": False}]}
    try:
        request_json(
            "POST",
            f"/api/v1/projects/{project_id}/identity-memberships/{identity_id}",
            token=admin_token,
            json_body=body,
        )
    except InfisicalBootstrapError as exc:
        message = str(exc).lower()
        if "already" in message or "duplicate" in message:
            return
        request_json(
            "PATCH",
            f"/api/v1/projects/{project_id}/identity-memberships/{identity_id}",
            token=admin_token,
            json_body=body,
        )


def create_client_secret(admin_token: str, identity_id: str, service_name: str) -> str:
    payload = request_json(
        "POST",
        f"/api/v1/auth/universal-auth/identities/{identity_id}/client-secrets",
        token=admin_token,
        json_body={
            "description": f"{service_name} local dev credential",
            "numUsesLimit": 0,
            "ttl": 0,
        },
    )
    client_secret = payload.get("clientSecret")
    if not isinstance(client_secret, str) or not client_secret:
        raise InfisicalBootstrapError(f"Client secret response for {service_name} is invalid.")
    return client_secret


def seed_secrets(admin_token: str, project_id: str) -> None:
    values = read_env_file(INPUT_ENV_PATH)
    values["OAUTH_KEK"] = get_existing_secret(admin_token, project_id, "OAUTH_KEK") or (
        generate_base64_kek()
    )

    for key in (*SEED_KEYS, "OAUTH_KEK"):
        value = values.get(key)
        if value:
            upsert_secret(admin_token, project_id, key, value)


def get_existing_secret(admin_token: str, project_id: str, name: str) -> str | None:
    try:
        payload = request_json(
            "GET",
            f"/api/v4/secrets/{name}?{secret_query(project_id)}",
            token=admin_token,
        )
    except InfisicalBootstrapError:
        return None
    secret = payload.get("secret")
    if isinstance(secret, dict) and isinstance(secret.get("secretValue"), str):
        return secret["secretValue"]
    return None


def upsert_secret(admin_token: str, project_id: str, name: str, value: str) -> None:
    body = {
        "projectId": project_id,
        "environment": ENV_SLUG,
        "secretValue": value,
        "secretPath": SECRET_PATH,
        "secretComment": "",
        "skipMultilineEncoding": False,
        "type": "shared",
    }
    existing = get_existing_secret(admin_token, project_id, name)
    method = "PATCH" if existing is not None else "POST"
    request_json(method, f"/api/v4/secrets/{name}", token=admin_token, json_body=body)


def verify_service_access(state: BootstrapState) -> dict[str, bool]:
    verification: dict[str, bool] = {}
    for service_name, identity in state.services.items():
        try:
            login = request_json(
                "POST",
                "/api/v1/auth/universal-auth/login",
                json_body={"clientId": identity.client_id, "clientSecret": identity.client_secret},
            )
            token = require_str(login, "accessToken")
            payload = request_json(
                "GET",
                f"/api/v4/secrets?{secret_query(state.project_id)}",
                token=token,
            )
            verification[service_name] = isinstance(payload.get("secrets"), list)
        except InfisicalBootstrapError:
            verification[service_name] = False
    return verification


def request_json(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    url = path if path.startswith("http") else f"{INFISICAL_URL}{path}"
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(json_body).encode("utf-8")

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise InfisicalBootstrapError(
            f"{method} {path} failed with HTTP {exc.code}: {redact(payload)}"
        ) from exc
    except OSError as exc:
        raise InfisicalBootstrapError(f"{method} {path} failed: {exc}") from exc

    if not payload:
        return {}
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise InfisicalBootstrapError(f"{method} {path} response was not a JSON object.")
    return decoded


def secret_query(project_id: str) -> str:
    return urlencode(
        {
            "projectId": project_id,
            "environment": ENV_SLUG,
            "secretPath": SECRET_PATH,
            "type": "shared",
            "viewSecretValue": "true",
            "expandSecretReferences": "true",
        }
    )


def write_output_env(state: BootstrapState) -> None:
    OUTPUT_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Dev-only local Infisical bootstrap output. Do not commit.",
        f"INFISICAL_URL={INFISICAL_URL}",
        f"INFISICAL_ADMIN_EMAIL={state.admin_email}",
        f"INFISICAL_ADMIN_PASSWORD={state.admin_password}",
        f"INFISICAL_ADMIN_TOKEN={state.admin_token}",
        f"INFISICAL_ORG_ID={state.org_id}",
        f"INFISICAL_ORG_SLUG={state.org_slug}",
        f"INFISICAL_PROJECT_ID={state.project_id}",
        f"INFISICAL_PROJECT_SLUG={state.project_slug}",
        f"INFISICAL_ENV={ENV_SLUG}",
    ]
    for service_name in SERVICE_NAMES:
        identity = state.services[service_name]
        prefix = env_key_prefix(service_name)
        lines.extend(
            [
                f"{prefix}_INFISICAL_CLIENT_ID={identity.client_id}",
                f"{prefix}_INFISICAL_CLIENT_SECRET={identity.client_secret}",
            ]
        )

    OUTPUT_ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with suppress(OSError):
        OUTPUT_ENV_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


def print_summary(state: BootstrapState, verification: Mapping[str, bool]) -> None:
    print("Infisical dev bootstrap complete.")
    print(f"Credentials file: {OUTPUT_ENV_PATH}")
    print(f"Admin: {state.admin_email} / {mask(state.admin_password)}")
    print(f"Organization: {state.org_slug} ({state.org_id})")
    print(f"Project: {state.project_slug} ({state.project_id}), env={ENV_SLUG}")
    for service_name in SERVICE_NAMES:
        identity = state.services[service_name]
        status = "PASS" if verification.get(service_name) else "FAIL"
        print(
            f"Identity {service_name}: id={identity.identity_id}, "
            f"client_id={mask(identity.client_id)}, verification={status}"
        )
    print("Secret values were not printed.")


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def extract_admin_token(payload: Mapping[str, Any]) -> str:
    identity = payload.get("identity") or payload.get("machineIdentity")
    if not isinstance(identity, dict):
        raise InfisicalBootstrapError("Bootstrap response did not include admin identity.")
    credentials = identity.get("credentials")
    if not isinstance(credentials, dict):
        raise InfisicalBootstrapError("Bootstrap response did not include admin credentials.")
    return require_str(credentials, "token")


def require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise InfisicalBootstrapError(f"Expected non-empty string field: {key}")
    return value


def env_key_prefix(service_name: str) -> str:
    return service_name.upper().replace("-", "_")


def generate_password() -> str:
    return secrets.token_urlsafe(32)


def mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def redact(value: str) -> str:
    return value.replace("\\", "\\\\")[:500]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InfisicalBootstrapError as error:
        print(f"Infisical bootstrap failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
