# """CLI: evaluate one case folder and print the Result as JSON.

#     python run.py cases/case1_feasible_even
# """

# from __future__ import annotations

# import json
# import sys

# from feasibility.engine import evaluate_offer
# from feasibility.models import load_case


# def main(argv: list[str]) -> int:
#     if len(argv) != 2:
#         print("usage: python run.py <case_dir>", file=sys.stderr)
#         return 2
#     client, offer, rules = load_case(argv[1])
#     result = evaluate_offer(client, offer, rules)
#     print(json.dumps(result.to_dict(), indent=2))
#     return 0


# if __name__ == "__main__":
#     raise SystemExit(main(sys.argv))

"""
run.py — CLI entry point.

Usage:
    python run.py cases/case1_feasible_even
    python run.py cases/case2_infeasible_minima
"""

import json
import sys
from pathlib import Path

from feasibility.models import load_case
from feasibility.engine import evaluate_offer


def main():
    if len(sys.argv) < 2:
        print("Usage: python run.py <case_directory>")
        sys.exit(1)

    case_dir = Path(sys.argv[1])
    if not case_dir.exists():
        print(f"Error: directory '{case_dir}' not found")
        sys.exit(1)

    client, offer, rules = load_case(case_dir)
    result = evaluate_offer(client, offer, rules)
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()