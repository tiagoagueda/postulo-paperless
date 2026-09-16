"""postulo-paperless: every document Postulo holds, kept in a Paperless-ngx archive.

Paperless-ngx is the self-hoster's document archive. This package registers a **store**
with Postulo: once a person connects it — the archive's address and an API token —
every new document Postulo produces or receives is posted to Paperless, filed with the
company as correspondent, the kind as document type, a tag, and a custom field that
leads back to the application. Local media stays the source of truth; Paperless gets a
copy, and an archive is for keeping: nothing is ever deleted there.

Every string a person reads is translated by this package itself, from ``locale/`` next to
this file; Postulo never carries a plugin's words.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    #: Read from the installed distribution rather than written here, so that the number
    #: Postulo shows on *Server settings → Plugins* is the number of the package somebody
    #: actually installed. Two places to write it is one place to forget: this said 0.1.0
    #: for the whole of 0.3.0's development (#2).
    __version__ = version("postulo-paperless")
except PackageNotFoundError:  # pragma: no cover - only when run from a source tree
    __version__ = "0.0.0"
