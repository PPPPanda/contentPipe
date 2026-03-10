from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from web.auth import hash_token


def test_hash_token_is_stable():
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != hash_token("def")
