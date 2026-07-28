#!/usr/bin/env python3
"""Backward-compatible alias for the margin-buy / SSE CLI."""

from __future__ import annotations

from .margin_buy_vs_sse_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
