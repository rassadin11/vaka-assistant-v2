"""Run the versioned prompt suite manually against OpenRouter."""

from __future__ import annotations

import asyncio

from core.llm_openrouter import OpenRouterProvider
from evals.scenarios_v1 import SCENARIOS


async def main() -> int:
    """Run all scenarios and return a shell-compatible status code."""

    llm = OpenRouterProvider()
    passed = 0
    for scenario in SCENARIOS:
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
    print(f"Score: {passed}/20")
    return 0 if passed >= 18 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
