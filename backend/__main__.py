"""Authoritative production module entry point for Message Automation (python -m backend)."""

import sys
from backend.cli import main

if __name__ == "__main__":
    # Default to 'start' command if no arguments are provided
    if len(sys.argv) == 1:
        sys.argv.append("start")
    main()
