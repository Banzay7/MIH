"""The web UI's translations: every Russian text the app shows has a version in each
interface language (i18n/<code>.js), with the same {n} placeholders."""

import json
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "mih" / "ui" / "static"
# Every language of the menu except Russian, the source text.
LANGS = ("en", "vi", "zh", "hi", "es", "fr", "ar", "bn", "pt", "ur", "id", "de", "ja", "mr",
         "te", "tr", "ta", "ko", "it")  # fmt: skip


def _dicts() -> dict[str, dict[str, str]]:
    out = {}
    for lang in LANGS:
        src = (STATIC / "i18n" / f"{lang}.js").read_text(encoding="utf-8")
        out[lang] = json.loads(re.search(rf"\.{lang} = (\{{.*\}});\s*$", src, re.S).group(1))
    return out


def _app() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _tips() -> set[str]:
    tips = re.search(r"const TIPS = \{(.*?)\n\};", _app(), re.S)
    return set(re.findall(r':\s*"([^"]+)"', tips.group(1))) if tips else set()


def _keys() -> set[str]:
    keys = {json.loads(f'"{k}"') for k in re.findall(r'\bt\("((?:[^"\\]|\\.)*)"', _app())}
    keys |= _tips()
    shell = (STATIC / "index.html").read_text(encoding="utf-8")
    keys |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', shell))
    # The help texts name buttons as {{Русский текст}}, which t() renders.
    for f in (STATIC / "help").glob("*.json"):
        keys |= set(re.findall(r"\{\{([^{}]+)\}\}", f.read_text(encoding="utf-8")))
    return keys


def test_menu_lists_every_language_main_three_first():
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    menu = re.search(r"const LANGS_UI = \{(.*?)\n\};", src, re.S).group(1)
    codes = re.findall(r"\b([a-z]{2}): \"", menu)
    assert codes[:3] == ["ru", "en", "vi"]
    assert sorted(codes) == sorted(("ru", *LANGS)) and len(codes) == 20
    locales = re.search(r"const LOCALES = \{(.*?)\n\};", src, re.S).group(1)
    assert sorted(re.findall(r"\b([a-z]{2}): \"", locales)) == sorted(codes)


def test_every_text_is_translated():
    keys = _keys()
    assert len(keys) > 300
    # Long tips texts are in English and Vietnamese; the other languages fall back to
    # English for them.
    tips = _tips()
    for lang, d in _dicts().items():
        need = keys if lang in ("en", "vi") else keys - tips
        assert not sorted(need - d.keys()), lang


def test_placeholders_match():
    for lang, d in _dicts().items():
        for ru, text in d.items():
            assert text.strip(), (lang, ru)
            marks = sorted(re.findall(r"\{\d\}", ru))
            assert marks == sorted(re.findall(r"\{\d\}", text)), (lang, ru)
