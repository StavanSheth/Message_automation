"""Authoritative production entry point for Message Automation."""

import sys
import argparse
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.bootstrap import build_production_app
from backend.events.logger import get_logger

logger = get_logger("main")


def main() -> None:
    parser = argparse.ArgumentParser(description="Instagram Message Automation Production Service")
    parser.add_argument("--db", type=str, default=None, help="Database path")
    parser.add_argument("--check-graph", action="store_true", help="Validate dependency graph and exit")
    args = parser.parse_args()

    app = build_production_app(db_path=args.db)

    is_valid, errors = app.validate_dependency_graph()
    if not is_valid:
        logger.error(f"Dependency graph validation failed: {errors}")
        print(f"FAILED: Dependency graph invalid: {errors}")
        sys.exit(1)

    print("Dependency graph verified: PASSED")
    if args.check_graph:
        sys.exit(0)

    res = app.start()
    print(f"Application start result: {res}")


if __name__ == "__main__":
    main()
