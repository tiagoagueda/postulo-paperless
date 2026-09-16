"""One door into Postulo, and this test is what keeps it one.

`postulo.plugins.api` is the whole of what a plugin may import; everything else in
`postulo` is this month's internals and may move in a patch release without a word. The
audit found this package reaching into `postulo.plugins.http` and `postulo.plugins.base`
for names that are now on the surface (#2), and the way not to drift back is to read the
source rather than to remember.

It is an AST walk rather than a grep so that `from postulo.plugins import http` and
`import postulo.plugins.base as base` are caught as readily as the obvious spelling, and
so that the word appearing in a docstring is not a failure.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "postulo_paperless"

#: The promise. Nothing else in `postulo` is one.
SURFACE = "postulo.plugins.api"


def modules_imported_from_postulo(source: str) -> list[str]:
    """Every `postulo…` module the source pulls in, however it spells the import."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # `from postulo.plugins import http` names a module in its `names`, and
            # `from postulo.plugins.api import client` names an object; treating both as
            # modules is what makes the first one visible here.
            found.append(node.module)
            found += [f"{node.module}.{alias.name}" for alias in node.names]
    return sorted({name for name in found if name == "postulo" or name.startswith("postulo.")})


@pytest.mark.parametrize("path", sorted(PACKAGE.rglob("*.py")), ids=lambda path: path.name)
def test_nothing_reaches_past_the_plugin_surface(path):
    reached = [
        name
        for name in modules_imported_from_postulo(path.read_text(encoding="utf-8"))
        if name != SURFACE and not name.startswith(f"{SURFACE}.")
    ]
    assert reached == [], (
        f"{path.name} imports {reached}. Everything a plugin may take from Postulo is in "
        f"{SURFACE}; see the wiki's 'Writing a plugin'."
    )
