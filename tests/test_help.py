"""The user guide and page tips (ui/static/help/<lang>.json): every language file has
the same pages, sections and points as the Russian source, and nothing is left empty."""

import json
import re
from pathlib import Path

HELP = Path(__file__).resolve().parents[1] / "src" / "mih" / "ui" / "static" / "help"
PAGES = {"home", "search", "channels", "favorites", "coverage", "status", "settings"}


def _load(lang: str) -> dict:
    return json.loads((HELP / f"{lang}.json").read_text(encoding="utf-8"))


def _shape(h: dict) -> tuple:
    return (sorted(h["tips"]), [(s["id"], len(s["items"])) for s in h["sections"]])


def _labels(h: dict) -> list:
    """The {{key}} control names of each point: a translation names the same controls."""
    texts = [*h["tips"].values()] + [i for s in h["sections"] for i in s["items"]]
    return [sorted(set(re.findall(r"\{\{([^{}]+)\}\}", t))) for t in texts]


def test_source_covers_every_page():
    ru = _load("ru")
    assert set(ru["tips"]) == PAGES
    assert {"start", "adult", "tv"} <= {s["id"] for s in ru["sections"]}


def test_every_language_matches_the_source():
    ru, ru_labels = _shape(_load("ru")), _labels(_load("ru"))
    files = sorted(HELP.glob("*.json"))
    assert {"ru", "en", "vi"} <= {f.stem for f in files}
    for f in files:
        h = _load(f.stem)
        assert _shape(h) == ru, f.stem
        assert _labels(h) == ru_labels, f.stem
        texts = [h["title"], h["intro"], h["contents"], h["more"], *h["tips"].values()]
        texts += [t for s in h["sections"] for t in [s["title"], *s["items"]]]
        assert all(isinstance(t, str) and t.strip() for t in texts), f.stem


def test_labels_are_texts_the_screen_shows():
    """{{key}} shows a control's name through t(), so the key must be one the app uses."""
    static = HELP.parent
    app = (static / "app.js").read_text(encoding="utf-8")
    keys = {json.loads(f'"{k}"') for k in re.findall(r'\bt\("((?:[^"\\]|\\.)*)"', app)}
    shell = (static / "index.html").read_text(encoding="utf-8")
    keys |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', shell))
    for f in sorted(HELP.glob("*.json")):
        used = set(re.findall(r"\{\{([^{}]+)\}\}", f.read_text(encoding="utf-8")))
        assert used <= keys, (f.stem, sorted(used - keys))
