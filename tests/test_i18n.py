"""The web UI's translations: every Russian text the app shows has an English and a
Vietnamese version, with the same {n} placeholders."""

import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "mih" / "ui" / "static"


def _dicts() -> dict[str, dict[str, str]]:
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    out = {}
    for lang in ("en", "vi"):
        body = re.search(rf"^{lang}: (\{{.*?^\}}),$", src, re.S | re.M).group(1)
        out[lang] = json.loads(body)
    return out


def _keys() -> set[str]:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    keys = {json.loads(f'"{k}"') for k in re.findall(r'\bt\("((?:[^"\\]|\\.)*)"', app)}
    tips = re.search(r"const TIPS = \{(.*?)\n\};", app, re.S).group(1)
    keys |= set(re.findall(r':\s*"([^"]+)"', tips))
    shell = (STATIC / "index.html").read_text(encoding="utf-8")
    keys |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', shell))
    return keys


def test_every_text_is_translated():
    keys = _keys()
    assert len(keys) > 300
    for lang, d in _dicts().items():
        assert not sorted(keys - d.keys()), lang


def test_placeholders_match():
    for d in _dicts().values():
        for ru, text in d.items():
            assert sorted(re.findall(r"\{\d\}", ru)) == sorted(re.findall(r"\{\d\}", text)), ru
