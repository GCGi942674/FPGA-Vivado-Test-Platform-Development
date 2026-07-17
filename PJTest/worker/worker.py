#!/usr/bin/env python3
"""Stable entry point for the PJTest worker service."""

from worker_core.main import main


if __name__ == "__main__":
    raise SystemExit(main())
