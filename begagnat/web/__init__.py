"""The local web UI: watches, the feed, and everything the CLI can print."""

from .app import create_app, serve

__all__ = ["create_app", "serve"]
