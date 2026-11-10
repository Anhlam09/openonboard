"""The project disclaimer: present on the site and in the README, and kept word for word in sync."""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "app" / "static" / "index.html").read_text()
README = (ROOT / "README.md").read_text()
BANNER = "Independent experimental project. Not for real-world account opening."


def _readme_disclaimer_paragraphs():
    section = README.split("## Independent project disclaimer", 1)[1].split("\n## ", 1)[0]
    return [p.strip() for p in section.strip().split("\n\n") if p.strip()]


def test_readme_has_a_full_disclaimer():
    paragraphs = _readme_disclaimer_paragraphs()
    assert len(paragraphs) == 3
    text = " ".join(paragraphs)
    assert "independent, open-source educational prototype" in text
    assert "not affiliated with, sponsored by, or endorsed by any banking software provider or financial institution" in text
    assert "not production-ready" in text and "may be simulated" in text
    # The License line keeps the Middesk and advice notes.
    assert "Not affiliated with Middesk. Not legal or compliance advice." in README


def test_site_shows_the_same_disclaimer_as_the_readme():
    for paragraph in _readme_disclaimer_paragraphs():
        assert paragraph in PAGE, f"site is missing: {paragraph[:60]}..."


def test_site_footer_and_banners_carry_the_short_notice():
    footer = re.search(r'<footer class="site-footer" id="disclaimer">(.*?)</footer>', PAGE, re.S)
    assert footer and BANNER in footer.group(1)
    assert "<summary>Independent project disclaimer</summary>" in footer.group(1)
    # Both the default demo banner and the in-browser banner include it.
    assert PAGE.count(BANNER) >= 3


def test_static_build_keeps_the_disclaimer(tmp_path):
    spec = importlib.util.spec_from_file_location("build_static", ROOT / "scripts" / "build_static.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    page = (module.build(tmp_path / "site") / "index.html").read_text()
    assert 'id="disclaimer"' in page and BANNER in page
