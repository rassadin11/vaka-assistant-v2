"""Unit tests for model-routing contracts."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.model_router import ModelRouter, RouteRequest, StaticModelRouter


@pytest.mark.parametrize(
    "route_request",
    [
        RouteRequest(task_type="interactive", user_plan="standard"),
        RouteRequest(task_type="interactive", user_plan="premium"),
        RouteRequest(task_type="background", user_plan="standard"),
        RouteRequest(task_type="background", user_plan="any-plan"),
    ],
)
def test_static_model_router_uses_configured_model_for_every_request(
    route_request: RouteRequest,
) -> None:
    router = StaticModelRouter("deepseek-chat")

    route = router.route(route_request)

    assert route.model == "deepseek-chat"
    assert route.budget_multiplier == Decimal(1)


def test_static_model_router_conforms_to_model_router_protocol() -> None:
    assert isinstance(StaticModelRouter("deepseek-chat"), ModelRouter)
