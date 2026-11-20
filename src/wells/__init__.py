"""Wells — model-agnostic agentic coding."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version


def _detect_version() -> str:
    """Single source of truth: the installed distribution metadata.

    pyproject.toml's ``version`` is the only place a release number is
    written. Reading it back here keeps ``wells --version``, the MCP server
    handshake, and the PyPI artifact from ever disagreeing (they did: three
    hardcoded copies drifted to 0.2.0 while the package was 0.3.0).

    A frozen PyInstaller bundle has metadata too (the dist-info is collected
    as a data file), but fall back to a marker rather than crashing if the
    environment is exotic — a wrong-but-present version string is never worth
    a failed startup.
    """
    try:
        return _pkg_version("wells-agent")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _detect_version()
