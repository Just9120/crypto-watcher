#!/usr/bin/env python3
"""Compatibility entrypoint; implementation lives in the cryptowatcher package."""

from cryptowatcher.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
