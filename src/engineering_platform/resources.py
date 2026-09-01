"""Access to resources owned by the installed Engineering Platform package.

Project roots contain consumer-owned state.  They are never used to locate
Engineering Platform defaults or metadata.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


class PackageResourceError(FileNotFoundError):
    """Raised when a required Engineering Platform package resource is absent."""


def package_text(name: str) -> str:
    """Read one required UTF-8 resource from the installed package.

    This deliberately has no checkout, working-directory, or project-root
    fallback.  A missing resource is an invalid package installation.
    """
    resource = files("engineering_platform").joinpath(name)
    if not resource.is_file():
        raise PackageResourceError(f"Engineering Platform package resource is missing: {name}")
    return resource.read_text(encoding="utf-8")


def package_path(name: str) -> Path:
    """Return a filesystem path for one required installed package resource.

    Engineering Platform wheels are installed as regular package directories;
    callers which require a path therefore receive the package-owned path,
    never a path inferred from a project checkout.
    """
    resource = files("engineering_platform").joinpath(name)
    if not resource.is_file():
        raise PackageResourceError(f"Engineering Platform package resource is missing: {name}")
    return Path(resource)
