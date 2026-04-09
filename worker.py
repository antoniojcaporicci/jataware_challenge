#!/usr/bin/env python3
"""Long-running worker entrypoint; equivalent to ``python -m nightwatch_worker``."""

from nightwatch_worker.worker_cli import main

if __name__ == "__main__":
    main()
