"""Persistent file logger for Wells. All errors go here with full tracebacks."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _log_dir() -> Path:
    d = Path.home() / ".wells" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_log_path() -> Path:
    return _log_dir() / "wells.log"


def _get_tool_log_path() -> Path:
    return _log_dir() / "tools.log"


def _setup() -> logging.Logger:
    log = logging.getLogger("wells")
    if log.handlers:
        return log

    log.setLevel(logging.DEBUG)

    # Rotating file: 1 MB per file, keep 3
    fh = RotatingFileHandler(
        _get_log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    log.addHandler(fh)
    return log


def _setup_tools() -> logging.Logger:
    """Separate rotating log for full tool-call I/O (name/args/result), kept apart
    from wells.log so verbose command output doesn't crowd out internal errors —
    and so the full text the model actually saw is always recoverable, not just
    the truncated one-liner the TUI prints for each round (see /log)."""
    log = logging.getLogger("wells.tools")
    if log.handlers:
        return log

    log.setLevel(logging.DEBUG)
    fh = RotatingFileHandler(
        _get_tool_log_path(), maxBytes=5_000_000, backupCount=2, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(fh)
    log.propagate = False
    return log


logger = _setup()
_tool_logger = _setup_tools()


def log_error(msg: str, exc: BaseException | None = None) -> None:
    """Log an error message, optionally with a full exception traceback."""
    if exc is not None:
        logger.error(msg, exc_info=exc)
    else:
        logger.error(msg)


def log_warning(msg: str) -> None:
    logger.warning(msg)


def log_info(msg: str) -> None:
    logger.info(msg)


def log_path() -> str:
    return str(_get_log_path())


def log_tool_result(name: str, args: dict, ok: bool, text: str) -> None:
    """Record one tool call's full, untruncated observation (what the model saw).

    This is the only place the complete text survives — the TUI/CLI only ever
    print a one-line activity summary for each round.
    """
    status = "OK" if ok else "FAIL"
    _tool_logger.info("=== %s %s args=%r ===\n%s", status, name, args, text)


def tool_log_path() -> str:
    return str(_get_tool_log_path())


def tail_tool_log(n: int = 10) -> list[str]:
    """Return the text of the last ``n`` tool-call entries, most recent last."""
    path = _get_tool_log_path()
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8", errors="replace")
    entries = raw.split("=== ")[1:]  # drop anything before the first entry marker
    return ["=== " + e.rstrip() for e in entries[-n:]]
