"""One door into Postulo, and this test is what keeps it one.

`postulo.plugins.api` is the whole of what a plugin may import; everything else in
`postulo` is this month's internals and may move in a patch release without a word. The
audit found this package reaching into `postulo.plugins.http` and `postulo.plugins.base`
for names that are now on the surface (#2), and the way not to drift back is to read the
source rather than to remember.

**The reading is the core's now** (postulo/postulo#229). This file used to hold its own AST
walk; so did postulo-apprise, postulo-helloworld and postulo-imap, and postulo-dav kept a
fourth inside its main test file. Five near-identical checks, no two agreeing about
relative imports or about what counted as reaching past — which is the drift a published
one exists to stop. `postulo.plugins.testing` is still an AST walk rather than a grep, so
`from postulo.plugins import http` and `import postulo.plugins.base as base` are caught as
readily as the obvious spelling, and the word appearing in a docstring is not a failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from postulo.plugins.testing import SURFACE, assert_imports_only_the_surface, imports_of

PACKAGE = "postulo_paperless"
SOURCE = Path(__file__).resolve().parents[1] / "src" / PACKAGE


def test_there_is_something_to_check():
    """A sanity check on the check: an empty package would make everything below pass."""
    assert len(list(SOURCE.rglob("*.py"))) >= 2


def test_nothing_reaches_past_the_plugin_surface():
    assert_imports_only_the_surface(SOURCE, package=PACKAGE)


def test_the_surface_is_what_this_plugin_actually_uses():
    """The other half: the surface is imported, rather than merely not reached past."""
    assert imports_of(SOURCE, package=PACKAGE) == {SURFACE}


@pytest.mark.parametrize(
    "name",
    [
        "DestinationRefused",
        "DocumentMetadata",
        "ExternalRef",
        "FieldSpec",
        "TestResult",
        "approve_host",
        "client",
    ],
)
def test_the_surface_promises_what_this_store_asks_of_it(name: str):
    """Not merely importable: on `__all__`, which is what the promise is made about.

    `DocumentMetadata` and `ExternalRef` are the store contract — what an archive is handed
    and what it gives back — and the two that #2 found this package taking from
    `postulo.plugins.base`.
    """
    from postulo.plugins import api

    assert name in api.__all__, f"{SURFACE} does not promise {name}"
