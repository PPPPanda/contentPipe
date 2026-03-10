from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from nodes import _strip_code_fence


def test_strip_code_fence_yaml_block():
    raw = "```yaml\nfoo: bar\n```"
    assert _strip_code_fence(raw) == "foo: bar"


def test_strip_code_fence_plain_yaml_marker():
    raw = "yaml\nfoo: bar"
    assert _strip_code_fence(raw) == "foo: bar"


def test_strip_code_fence_uppercase_json_marker():
    raw = 'JSON\n{"a": 1}'
    assert _strip_code_fence(raw) == '{"a": 1}'
