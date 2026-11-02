"""The user guide and page tips (ui/static/help/<lang>.json): every language file has
the same pages, sections and points as the Russian source, and nothing is left empty."""

import json
from pathlib import Path

HELP = Path(__file__).resolve().parents[1] / "src" / "mih" / "ui" / "static" / "help"
PAGES = {"home", "search", "channels", "favorites", "coverage", "status", "settings"}


def _load(lang: str) -> dict:
    return json.loads((HELP / f"{lang}.json").read_text(encoding="utf-8"))


def _shape(h: dict) -> tuple:
    return (sorted(h["tips"]), [(s["id"], len(s["items"])) for s in h["sections"]])


def test_source_covers_every_page():
    ru = _load("ru")
    assert set(ru["tips"]) == PAGES
    assert {"start", "adult", "tv"} <= {s["id"] for s in ru["sections"]}


def test_every_language_matches_the_source():
    ru = _shape(_load("ru"))
    files = sorted(HELP.glob("*.json"))
    assert {"ru", "en", "vi"} <= {f.stem for f in files}
    for f in files:
        h = _load(f.stem)
        assert _shape(h) == ru, f.stem
        texts = [h["title"], h["intro"], h["contents"], h["more"], *h["tips"].values()]
        texts += [t for s in h["sections"] for t in [s["title"], *s["items"]]]
        assert all(isinstance(t, str) and t.strip() for t in texts), f.stem
