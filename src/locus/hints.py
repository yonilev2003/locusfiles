"""Install and reinstall commands that match the installed version.

A pre-release (0.2.0rc3) is not what `uv` or `pipx` pick by default, so a hint that tells the user to reinstall without
saying so would put them back on the older stable release. While the installed version is a pre-release the commands
carry `--prerelease allow` (uv) or `--pip-args=--pre` (pipx); for a final release they are unchanged.
"""

from __future__ import annotations

import re


def is_prerelease(version: str | None = None) -> bool:
    """True for a version with an alpha, beta, rc or dev part ("0.2.0rc3", "0.2.0.dev1"); default: the installed one."""
    if version is None:
        from . import __version__ as version
    return bool(
        re.search(r"(?:a|b|c|rc|alpha|beta|pre|preview|dev)[.\-_]?\d*", re.sub(r"^v?\d+(?:\.\d+)*", "", version))
    )


def uv_install(extra: str, *, reinstall: bool = True) -> str:
    """`uv tool install ... "locusfiles[extra]"`, with `--prerelease allow` while this version is a pre-release."""
    flag = " --prerelease allow" if is_prerelease() else ""
    return f'uv tool install {"--reinstall " if reinstall else ""}--python 3.12 "locusfiles[{extra}]"{flag}'


def pipx_install(extra: str) -> str:
    """`pipx install --force "locusfiles[extra]"`, with `--pip-args=--pre` while this version is a pre-release."""
    flag = " --pip-args=--pre" if is_prerelease() else ""
    return f'pipx install --force "locusfiles[{extra}]"{flag}'


def pipx_reinstall() -> str:
    """`pipx reinstall locusfiles`: it keeps the original install options, so no flag is added."""
    return "pipx reinstall locusfiles"
