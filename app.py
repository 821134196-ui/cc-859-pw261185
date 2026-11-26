#!/usr/bin/env python3
"""Convenience entry point: ``python app.py [--port 8765]``.

Equivalent to ``python -m relocheck serve``.  Launches the zero-dependency
web workbench with the built-in deterministic demo databases.
"""
from relocheck.server import main

if __name__ == "__main__":
    main()
