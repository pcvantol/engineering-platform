"""Installed EP Server Console presentation resources.

This module owns only package resources and stable presentation identifiers.
It has no listener, route, project, queue, execution or persistence authority.
"""
from __future__ import annotations

from pathlib import Path


ASSET_DIRECTORY = Path(__file__).with_name("assets")
APP_ICON_DARK = "operations-console/apple-touch-icon-dark.png"
APP_ICON_LIGHT = "operations-console/apple-touch-icon-light.png"
WEB_MANIFEST = "operations-console/manifest.webmanifest"
