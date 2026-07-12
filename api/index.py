"""Vercel serverless entrypoint.

Vercel's Python runtime looks for an ASGI app named `app` in a module under
api/, and vercel.json rewrites every path here — so this one function serves the
whole thing: the chat, the icons, and the nightly cron endpoint.

The `jim` package is a real installed dependency (requirements.txt installs the
project itself), not a sys.path hack, so its package data — the migrations and
the home-screen icons — ships with the bundle.
"""

from jim.app import app

__all__ = ["app"]
