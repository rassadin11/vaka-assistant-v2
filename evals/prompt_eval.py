"""Run the versioned prompt suite manually against OpenRouter."""

from __future__ import annotations

import asyncio
import math
import os

from core.llm_openrouter import DEFAULT_OPENROUTER_MODEL, OpenRouterProvider
from evals.scenarios_v1 import build_scenarios


async def main() -> int:
    """Run all scenarios and return a shell-compatible status code."""

    prompt_version = os.getenv("EVAL_PROMPT_VERSION", "v1")
    scenarios = build_scenarios(prompt_version)
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    print(f"Model: {model}; prompt version: {prompt_version}; scenarios: {len(scenarios)}")

    llm = OpenRouterProvider()
    passed = 0
    for scenario in scenarios:
        response = None
        try:
            response = await llm.generate(scenario.messages, tools=scenario.tools, temperature=0)
            ok, reason = scenario.check(response)
        except Exception as exc:
            ok, reason = False, f"request failed: {exc}"
        status = "PASS" if ok else "FAIL"
        print(f"{status} {scenario.id}: {reason}")
        if not ok and response is not None:
            print(f"     answer: {response.message.content!r}")
            if response.message.tool_calls:
                calls = [(c.name, c.arguments_json) for c in response.message.tool_calls]
                print(f"     tool_calls: {calls}")
        passed += int(ok)
    threshold = math.ceil(0.9 * len(scenarios))
    print(f"Score: {passed}/{len(scenarios)} (threshold {threshold})")
    return 0 if passed >= threshold else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
