"""Skeleton smoke test: packages import, Python version matches the plan."""

import sys

import core
import gateway
import tools
import worker


def test_python_version() -> None:
    assert sys.version_info[:2] >= (3, 12)


def test_packages_importable() -> None:
    for pkg in (core, gateway, worker, tools):
        assert pkg.__doc__
