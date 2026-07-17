#!/usr/bin/env python3
"""Stable entry point for the PJTest scheduler service."""

from scheduler_core.main import main


if __name__ == "__main__":
    raise SystemExit(main())
