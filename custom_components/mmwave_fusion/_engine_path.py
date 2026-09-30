"""Locate the mmwave-engine package for this integration."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_mmwave_engine() -> None:
    if "mmwave_engine" in sys.modules:
        return
    try:
        import mmwave_engine  # noqa: F401
    except ImportError:
        pass
    else:
        return
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "mmwave-engine",
        here.parents[2] / "mmwave-engine",
        Path("/config/mmwave-engine"),
    ]
    for path in candidates:
        if (path / "mmwave_engine" / "__init__.py").is_file():
            sys.path.insert(0, str(path))
            return
    raise ImportError(
        "mmwave_engine was not found. Install mmwave-engine, or mount that "
        "repository at /config/mmwave-engine."
    )
