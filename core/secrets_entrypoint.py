"""Load Infisical secrets into the process environment before execing a service."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, MutableMapping, Sequence

from core.secrets import InfisicalSecretsProvider


def parse_command(argv: Sequence[str]) -> list[str]:
    """Parse the command following the mandatory ``--`` separator."""

    parser = argparse.ArgumentParser(
        description="Load Infisical secrets and execute a command.",
        usage="python -m core.secrets_entrypoint -- <command> [args ...]",
    )
    if not argv or argv[0] != "--":
        parser.error("a command following '--' is required")
    command = list(argv[1:])
    if not command:
        parser.error("a command following '--' is required")
    return command


def merge_secrets(
    environ: MutableMapping[str, str], secrets: Mapping[str, str]
) -> tuple[int, tuple[str, ...]]:
    """Add absent secrets to ``environ`` and preserve existing environment values."""

    added: list[str] = []
    for name, value in secrets.items():
        if name not in environ:
            environ[name] = value
            added.append(name)
    return len(added), tuple(added)


def run(
    command: Sequence[str],
    *,
    provider: InfisicalSecretsProvider | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> int:
    """Load secrets fail-closed, then replace this process with ``command``."""

    target_environ = os.environ if environ is None else environ
    try:
        secrets = (provider if provider is not None else InfisicalSecretsProvider()).list_all()
        added_count, added_names = merge_secrets(target_environ, secrets)
    except Exception as exc:
        # Secret values never appear in provider error messages, only API/auth
        # failures; include the cause so a crash-looping container is debuggable.
        print(f"Infisical secret loading failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        f"Loaded {len(secrets)} Infisical secrets; added {added_count}: "
        f"{', '.join(sorted(added_names)) or 'none'}.",
        file=sys.stderr,
    )
    try:
        os.execvp(command[0], list(command))
    except OSError:
        print("Service command could not be executed.", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the secrets entrypoint command-line interface."""

    command = parse_command(sys.argv[1:] if argv is None else argv)
    return run(command)


if __name__ == "__main__":
    raise SystemExit(main())
