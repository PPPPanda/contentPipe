from __future__ import annotations

import logging
import os

_LEVEL = os.environ.get("CONTENTPIPE_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, _LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
