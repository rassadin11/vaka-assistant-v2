"""Contracts for selecting an LLM model and task budget."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """Inputs used to choose a model for one task."""

    task_type: Literal["interactive", "background"]
    user_plan: str


@dataclass(frozen=True, slots=True)
class ModelRoute:
    """The selected model and its task-budget adjustment."""

    model: str
    budget_multiplier: Decimal


@runtime_checkable
class ModelRouter(Protocol):
    """Select a model route for a task."""

    def route(self, request: RouteRequest) -> ModelRoute:
        """Return the model route for ``request``."""


@dataclass(frozen=True, slots=True)
class StaticModelRouter:
    """Route every task to one configured model."""

    model: str

    def route(self, request: RouteRequest) -> ModelRoute:
        """Return the configured model without applying routing rules."""

        del request
        return ModelRoute(model=self.model, budget_multiplier=Decimal(1))
