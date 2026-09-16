"""This plugin's own catalogues, held to the rule Postulo holds its own to.

Postulo never translates a plugin's strings, so Postulo's release gate cannot see these;
this is the same gate, here (postulo/postulo#187). English is the source, French and
European Portuguese are filled while working, and the twenty-four European Union
catalogues are complete in a published version -- `uv run pytest -m release`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from postulo.core import messages_tool
except ImportError:  # pragma: no cover - only against a Postulo older than 0.3
    messages_tool = None

#: What a missing catalogue tool means, and what to do about it.
NO_TOOL = (
    "the catalogue tool arrived with Postulo 0.3; this repository's dev dependency "
    "pins the core, so seeing this means the pin has slipped"
)

REPO = Path(__file__).resolve().parents[1]

#: Filled while working, so a change is seen in three languages.
WORKING = ("fr-fr", "pt-pt")
#: The twenty-four, minus the source. Complete in a published version, not in every commit.
EUROPEAN_UNION = (
    "bg",
    "cs",
    "da",
    "de",
    "el",
    "es",
    "et",
    "fi",
    "fr-fr",
    "ga",
    "hr",
    "hu",
    "it",
    "lt",
    "lv",
    "mt",
    "nl",
    "pl",
    "pt-pt",
    "ro",
    "sk",
    "sl",
    "sv",
)


@pytest.fixture
def tool(request):
    """The catalogue tool -- skipped past while working, never at a release.

    `importorskip` used to stand here, and a skip is the wrong answer to "the core this was
    pinned to does not have the tool": the release gate went green by running nothing at all
    (#2). Under `-m release` a missing tool is a failure, which is what a gate is for.
    """
    if messages_tool is None:
        if request.node.get_closest_marker("release"):
            pytest.fail(NO_TOOL)
        pytest.skip(NO_TOOL)
    messages_tool.use(REPO)
    return messages_tool


def missing(tool, code: str) -> list[str]:
    catalogue = tool.parse(tool.po_path(code).read_text(encoding="utf-8"))
    return [m.msgid for m in catalogue.messages.values() if not m.translated]


def test_the_catalogues_are_current(tool):
    assert tool.cmd_extract(check=True) == 0, "run `uv run postulo-messages extract`"


def test_placeholders_and_plural_forms_agree(tool):
    assert tool.cmd_check() == 0


def test_every_language_postulo_offers_has_a_catalogue(tool):
    absent = [code for code in tool.translated_languages() if not tool.po_path(code).is_file()]
    assert absent == [], "run `uv run postulo-messages extract`"


@pytest.mark.parametrize("code", WORKING)
def test_the_working_languages_are_complete(tool, code):
    assert missing(tool, code) == []


@pytest.mark.release
@pytest.mark.parametrize("code", EUROPEAN_UNION)
def test_every_european_union_language_stays_complete(tool, code):
    gaps = missing(tool, code)
    assert gaps == [], f"{code}: {len(gaps)} untranslated, e.g. {gaps[:3]}"
