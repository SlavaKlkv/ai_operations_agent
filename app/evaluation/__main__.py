"""python -m app.evaluation — запустить набор из терминала или из CI.

По умолчанию используется детерминированный агент, потому что это базовый уровень:
запуск с моделью должен побить эти числа, чтобы заслужить вызов API. --llm
прогоняет те же сценарии через настроенный провайдер.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.logging import configure_logging
from app.evaluation.runner import render_report, run_suite
from app.evaluation.scenarios import SUITE, by_name


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app.evaluation", description=__doc__)
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Use the configured LLM instead of the deterministic baseline.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        metavar="NAME",
        help="Run only this scenario. Repeatable.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results.")
    parser.add_argument("--quiet", action="store_true", help="Suppress the agent's own log output.")
    return parser.parse_args(argv)


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging("ERROR" if args.quiet or args.json else "INFO")

    scenarios = [by_name(n) for n in args.scenario] if args.scenario else list(SUITE)
    score = await run_suite(scenarios, use_llm=args.llm)

    if args.json:
        print(json.dumps(score.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_report(score))

    # Ненулевой код завершения позволяет использовать проверку как барьер в CI.
    return 0 if score.passed == score.total else 1


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
