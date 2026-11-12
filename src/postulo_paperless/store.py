"""The store: post each document to Paperless-ngx, filed so it can be found again.

Paperless consumes a document asynchronously — ``POST /api/documents/post_document/``
answers with a task id, and the document exists once the task has run. ``put`` first asks
whether Paperless already holds the file, by its MD5 checksum, and uploads only when it does
not (#27); then it waits a short while for the task, and if Paperless is slower than that, it
raises, and Postulo's scheduler tries again later. That retry finds the document by its
checksum without uploading it again, and where the question cannot be asked (a Paperless
older than 1.16), the upload is still safe: Paperless refuses a document whose checksum it
already holds and names the one it has, which this plugin reads as success. A document is
never archived twice, however many times it is offered.

The correspondent, document type, tag and custom field are looked up by name once and
remembered for a few minutes (#27), so a scheduler pass filing fifty documents for the same
companies asks a handful of times rather than a hundred and fifty.

What is filed, all created on the Paperless side when missing: the **correspondent** is
the company; the **document type** is the kind — CV, cover letter, certificate…; one
**tag** marks everything that came from Postulo; and a **custom field** holds the
application's address on the Postulo instance, so a document found in Paperless leads
back to where it was used. Withdrawing or declining an application deletes nothing here:
an archive is for keeping.

Every request goes through ``postulo.plugins.api.client()``, so the instance's
destination policy applies — a Paperless on the LAN needs the operator's
``POSTULO_CONNECTIONS_ALLOW_PRIVATE`` — and so do its timeouts and user agent.

Everything this module takes from Postulo comes from ``postulo.plugins.api`` and nothing
else: that module is the promise, and the rest of ``postulo`` is this month's internals
(#2). ``tests/test_surface.py`` fails on a second door.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import time
from urllib.parse import urlsplit

import httpx
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext
from postulo.plugins.api import (
    BodyRefused,
    ConnectionUnusable,
    DestinationRefused,
    DocumentMetadata,
    ExternalRef,
    FieldSpec,
    TestResult,
    approve_host,
    client,
    deadline_in,
    read_body,
    until,
)

from . import __version__

#: How long ``put`` waits for Paperless to consume a document before leaving it to the
#: next scheduler pass. Consumption usually takes a few seconds; OCR of a scan takes
#: longer, and the scheduler, or a person's *Send now*, must not hang for it. Short, because
#: waiting longer saves nothing (#27): the next attempt finds the document by its checksum
#: and does not upload it again. Postulo's own budget for a pass of copies
#: (postulo/postulo#1172) never cuts a copy off once started, so this is also most of what
#: one copy can add to a pass beyond that budget.
WAIT_SECONDS = 5.0
POLL_SECONDS = 1.0
#: Uploading a document may take longer than the default request timeout.
UPLOAD_TIMEOUT = 60.0
#: The most any one request may take, answer included, from the first byte sent to the last
#: read: ``timeout`` bounds each wait for the network, not their sum, and Paperless is a server
#: the account holder named (#16). Handed to ``client(total=...)``; the upload gets
#: ``UPLOAD_TIMEOUT`` instead.
REQUEST_SECONDS = 30.0
#: The most of one answer that is read. These are small JSON documents (a page of one row, a
#: task, a document's fields); a megabyte is generous, and a server sending more is not
#: Paperless (#16).
MAX_BODY = 1024 * 1024
#: What is asked for, because ``read_body`` unpacks these two and refuses any other coding.
ACCEPT_ENCODING = "gzip, deflate"
#: The longest the remote server's own words are allowed to run in a message (#14).
DETAIL_CHARACTERS = 120
#: How long an id found or made by ``Archive.ensure`` is remembered (#27): about one
#: scheduler pass, so that a batch of documents for the same company asks once, and short
#: enough that a correspondent renamed or deleted in Paperless is looked up again soon.
LOOKUP_SECONDS = 300.0
#: The most ids remembered at once; past it the memory starts again. A few per connection.
LOOKUP_ENTRIES = 1024
#: What Paperless's ``post_document`` calls each field ``put`` fills with an id, against the
#: table ``ensure`` found it in. A 400 naming one is an id Paperless no longer has.
FIELD_TABLES = {
    "correspondent": "correspondents",
    "document_type": "document_types",
    "tags": "tags",
}

logger = logging.getLogger(__name__)

#: (address, account, table, name in lower case) → (id, until when, by ``time.monotonic``).
#: Module-level so that it outlives one ``put``: the scheduler files each document through a
#: new ``Archive``. The account is a digest of the token, because what one Paperless user can
#: see of the tables another may not.
_lookups: dict[tuple[str, str, str, str], tuple[int, float]] = {}

#: The id Paperless closes its duplicate message with: "... It is a duplicate of <title>
#: (#<id>)." The title comes first and is the person's own text, so a ``#`` in it comes before
#: the id and never after (#11).
_DUPLICATE_ID = re.compile(r"\(#(\d+)\)")
_ANY_ID = re.compile(r"#(\d+)")


class PaperlessError(Exception):
    """Paperless answered, and the answer was no. The message is for the person."""


class FileRefused(PaperlessError):
    """Paperless will not take this file, and offering it again will not change that (#14)."""


class StillConsuming(PaperlessError):
    """The task has not finished within the wait; the next pass will find the document."""


def _base(config: dict) -> str:
    return str(config.get("url", "")).rstrip("/")


def forget_lookups() -> None:
    """Forget every remembered id, as a new process would."""
    _lookups.clear()


def md5_of(file) -> str:
    """The file's MD5, which is the checksum Paperless keeps of a document's original.

    Read from the start and put back there, for the upload that may follow. Empty when the
    file cannot be rewound, and then nothing is looked up: the upload reads it as it is.
    """
    try:
        file.seek(0)
    except (AttributeError, OSError, ValueError):
        return ""
    # Not a security use: Paperless names a document by this, and this is only how it is asked.
    digest = hashlib.md5(usedforsecurity=False)
    for chunk in iter(lambda: file.read(1024 * 1024), b""):
        digest.update(chunk)
    file.seek(0)
    return digest.hexdigest()


def _is_public(address: str) -> bool:
    """Whether an address ``approve_host`` handed back is out on the internet.

    Not a second policy: Postulo has already decided the address may be dialled. This only
    tells apart the archive on somebody's own network from one a token would have to cross
    the internet to reach.
    """
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:  # pragma: no cover - approve_host answers with an address
        return True


def _headers(config: dict) -> dict:
    return {"Authorization": f"Token {config.get('token', '')}", "Accept": "application/json"}


def _one_line(text: object, limit: int = DETAIL_CHARACTERS) -> str:
    """What a server said, on one line and no longer than ``limit``.

    The message is recorded on the copy and written to the operator's log, and a log line is
    one place a newline from a stranger must never land (#14).
    """
    return " ".join(str(text).split())[:limit]


def _messages(body: object) -> list[str]:
    """Every message in a Django REST Framework error body, field names kept.

    ``{"detail": "..."}`` for authentication and permission errors; for a validation error a
    mapping of field name to a list of messages (``{"document": ["File type ... not
    supported"]}``), which nests for a nested serializer; sometimes a bare list.
    """
    if isinstance(body, str):
        return [body]
    if isinstance(body, list):
        return [message for item in body for message in _messages(item)]
    if isinstance(body, dict):
        if "detail" in body:
            return _messages(body["detail"])
        found = []
        for field, value in body.items():
            found.extend(f"{field}: {message}" for message in _messages(value))
        return found
    return []


def _explain(response: httpx.Response) -> str:
    try:
        detail = " ".join(_messages(response.json()))
    except ValueError:
        detail = response.text
    detail = _one_line(detail)
    return f"HTTP {response.status_code}" + (f": {detail}" if detail else "")


def _duplicate_id(result: str) -> int | None:
    """The document a task says already exists: the last ``(#id)``, else the last ``#id``."""
    for pattern in (_DUPLICATE_ID, _ANY_ID):
        found = pattern.findall(result)
        if found:
            return int(found[-1])
    return None


def _refuses_the_file(response: httpx.Response) -> bool:
    """Whether a 400 is about the uploaded file itself: its errors are on the ``document`` field."""
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "document" in body and "detail" not in body


def _is_absolute(url: str) -> bool:
    """Whether Paperless's URL field would take it: a scheme it knows and a host."""
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def compose_title(metadata: DocumentMetadata) -> str:
    """ "CV — Company — Role — date", or as much of that as the document has."""
    primary = metadata.kind_label if metadata.origin == "render" else metadata.title
    when = metadata.sent_on or metadata.created_at.date()
    parts = [primary, metadata.company, metadata.role, when.isoformat()]
    return " — ".join(part for part in parts if part)


class Archive:
    """One Paperless, one token: the handful of API calls the store needs."""

    def __init__(self, config: dict, *, client: httpx.Client) -> None:
        self.base = _base(config)
        self.client = client
        self.headers = _headers(config)
        token = str(config.get("token", "")).encode()
        self.account = hashlib.sha256(token).hexdigest()[:16]
        #: The keys whose ids this archive took from memory rather than from Paperless.
        self.remembered: list[tuple[str, str, str, str]] = []

    # ------------------------------------------------------------ plumbing

    def _send(self, method: str, path: str, *, seconds: float | None = None, **kwargs):
        """One request, its answer read no further than ``MAX_BODY`` and ``seconds`` (#16).

        Streamed, because ``client.get`` has read the whole body before it returns. A redirect is
        never followed: the address is the one the person typed, and a server that answers with
        another is not sent the token a second time, wherever it points.
        """
        deadline = deadline_in(REQUEST_SECONDS if seconds is None else seconds)
        try:
            with (
                until(deadline),
                self.client.stream(
                    method,
                    f"{self.base}/api/{path}",
                    headers={**self.headers, "Accept-Encoding": ACCEPT_ENCODING},
                    follow_redirects=False,
                    **kwargs,
                ) as response,
            ):
                body = read_body(response, limit=MAX_BODY, deadline=deadline)
                status = response.status_code
        except BodyRefused as error:
            raise PaperlessError(
                str(_("Paperless answered with more than this plugin reads, or too slowly."))
            ) from error
        return httpx.Response(status, content=body)

    def _refuse(self, response: httpx.Response) -> PaperlessError:
        """The exception for an answer that was not the one wanted.

        A 401 is the other side finished with the token (revoked, regenerated, the account
        removed): trying again changes nothing, so the connection is switched off once with a
        reason and the rest of the queue waits, instead of every document failing six times
        (#13). A 403 is one missing permission in Paperless, fixed there, so it stays an
        ordinary failure that is tried again.
        """
        if response.status_code == 401:
            raise ConnectionUnusable(
                str(
                    _(
                        "Paperless no longer accepts the API token (%(reason)s). "
                        "Enter a current one, then switch the connection on again."
                    )
                )
                % {"reason": _explain(response)}
            )
        return PaperlessError(_explain(response))

    def _get(self, path: str, **params) -> dict | list:
        response = self._send("GET", path, params=params)
        if response.status_code != 200:
            raise self._refuse(response)
        return response.json()

    def _post(self, path: str, **kwargs) -> httpx.Response:
        response = self._send("POST", path, **kwargs)
        if response.status_code not in (200, 201):
            raise self._refuse(response)
        return response

    # ---------------------------------------------------------- the calls

    def count_documents(self) -> int:
        return int(self._get("documents/", page_size=1)["count"])

    def ensure(self, kind: str, name: str, **extra) -> int:
        """The id of the correspondent, document type, tag or custom field called ``name``.

        Created when missing. Names are matched without regard to case, so "Black Mesa"
        and "black mesa" are the one correspondent. Remembered for ``LOOKUP_SECONDS`` (#27),
        and forgotten sooner if Paperless refuses the id (`forget`).
        """
        key = (self.base, self.account, kind, name.lower())
        now = time.monotonic()
        remembered = _lookups.get(key)
        if remembered is not None and remembered[1] > now:
            self.remembered.append(key)
            return remembered[0]
        found = self._get(f"{kind}/", name__iexact=name, page_size=1)
        results = found.get("results", []) if isinstance(found, dict) else []
        if results:
            found_id = int(results[0]["id"])
        else:
            found_id = int(self._post(f"{kind}/", json={"name": name, **extra}).json()["id"])
        if len(_lookups) >= LOOKUP_ENTRIES:
            _lookups.clear()
        _lookups[key] = (found_id, now + LOOKUP_SECONDS)
        return found_id

    def forget(self, kinds: set[str] | None = None) -> None:
        """Forget the ids this archive took from memory, of ``kinds`` or all of them.

        Called when Paperless refuses one with a 400: it was deleted or merged there, and the
        next document looks the name up again rather than being refused for the same reason.
        """
        for key in self.remembered:
            if kinds is None or key[2] in kinds:
                _lookups.pop(key, None)

    def find_by_checksum(self, checksum: str) -> int | None:
        """The document Paperless already holds whose original has this MD5, if any (#27).

        ``checksum__iexact`` arrived in Paperless-ngx 1.16, and an older one ignores a filter it
        does not know and answers with every document. So a match counts only when exactly one
        came back and its metadata names this checksum; anything else, or a refusal, is "not
        known", and the document is uploaded as it always was. A token Paperless no longer
        accepts is still said at once.
        """
        try:
            response = self._send(
                "GET", "documents/", params={"checksum__iexact": checksum, "page_size": 1}
            )
            if response.status_code == 401:
                raise self._refuse(response)
            body = response.json() if response.status_code == 200 else {}
            results = body.get("results") if isinstance(body, dict) else None
            if not results or body.get("count") != 1:
                return None
            candidate = int(results[0]["id"])
            response = self._send("GET", f"documents/{candidate}/metadata/")
            if response.status_code == 401:
                raise self._refuse(response)
            metadata = response.json() if response.status_code == 200 else {}
        except (PaperlessError, ValueError, TypeError, KeyError):
            return None
        original = metadata.get("original_checksum") if isinstance(metadata, dict) else None
        return candidate if str(original or "").lower() == checksum.lower() else None

    def post_document(self, file, metadata: DocumentMetadata, fields: dict) -> str:
        """Offer the file to the consumer. Returns the task id Paperless gave back."""
        data = {key: value for key, value in fields.items() if value not in (None, "", [])}
        response = self._send(
            "POST",
            "documents/post_document/",
            seconds=UPLOAD_TIMEOUT,
            data=data,
            files={"document": (metadata.filename, file, metadata.content_type)},
            timeout=UPLOAD_TIMEOUT,
        )
        if response.status_code == 400 and _refuses_the_file(response):
            # Paperless looked at the file and will not consume it (a type it has no parser
            # for). It is the same answer however often it is asked, so say so once (#14).
            raise FileRefused(_explain(response))
        if response.status_code == 400:
            # Most likely an id remembered from an earlier document that Paperless no longer
            # has: forget the ones it names, or every one this document used, and the next
            # attempt asks again.
            try:
                body = response.json()
            except ValueError:
                body = None
            named = {table for field, table in FIELD_TABLES.items() if field in (body or {})}
            self.forget(named or None)
        if response.status_code != 200:
            raise self._refuse(response)
        body = response.json()
        return str(body if not isinstance(body, dict) else body.get("task_id", body))

    def task(self, task_id: str) -> dict | None:
        found = self._get("tasks/", task_id=task_id)
        if isinstance(found, dict):
            found = found.get("results", [])
        return found[0] if found else None

    def wait_for(self, task_id: str, *, seconds: float | None = None) -> int:
        """The document id the task produced, or the one it said already existed."""
        deadline = time.monotonic() + (WAIT_SECONDS if seconds is None else seconds)
        while True:
            task = self.task(task_id)
            status = (task or {}).get("status", "PENDING")
            if status == "SUCCESS":
                related = (task or {}).get("related_document")
                if related:
                    return int(related)
                existing = _duplicate_id(str((task or {}).get("result", "")))
                if existing:
                    return existing
                raise PaperlessError(str(_("Paperless consumed the document but gave no id.")))
            if status == "FAILURE":
                result = str((task or {}).get("result", ""))
                existing = _duplicate_id(result)
                if "duplicate" in result.lower() and existing:
                    # Paperless already holds this exact file. That is the outcome we
                    # wanted, and it is why offering a document twice is harmless.
                    return existing
                raise PaperlessError(
                    str(_("Paperless could not consume the document: %(reason)s"))
                    % {"reason": _one_line(result, 300) or status}
                )
            if time.monotonic() >= deadline:
                raise StillConsuming(
                    str(_("Paperless is still consuming the document; it will be checked again."))
                )
            time.sleep(POLL_SECONDS)

    def set_custom_field(self, document_id: int, field_id: int, value: str) -> None:
        """Write one custom field, keeping whatever other fields the document already has."""
        current = self._get(f"documents/{document_id}/")
        fields = [
            entry
            for entry in (current.get("custom_fields") or [])
            if int(entry.get("field", -1)) != field_id
        ]
        fields.append({"field": field_id, "value": value})
        response = self._send("PATCH", f"documents/{document_id}/", json={"custom_fields": fields})
        if response.status_code == 400:
            self.forget({"custom_fields"})
        if response.status_code != 200:
            raise self._refuse(response)


class PaperlessStore:
    name = "paperless"
    version = __version__
    kind = "store"
    label = gettext_lazy("Paperless-ngx")

    def config_fields(self) -> list[FieldSpec]:
        return [
            FieldSpec(
                "url",
                str(_("Paperless address")),
                type="url",
                help=str(_("Where you open Paperless, e.g. https://paperless.example.org.")),
            ),
            FieldSpec(
                "token",
                str(_("API token")),
                type="password",
                secret=True,
                help=str(_("From your Paperless profile. Stored encrypted; never shown again.")),
            ),
            FieldSpec(
                "tag",
                str(_("Tag")),
                type="text",
                required=False,
                default="postulo",
                help=str(_("Put on every document that came from Postulo. Leave blank for none.")),
            ),
            FieldSpec(
                "custom_field",
                str(_("Custom field for the application's link")),
                type="text",
                required=False,
                default="Postulo",
                help=str(
                    _(
                        "A URL field on each document leading back to the application here, "
                        "created in Paperless if missing. Leave blank for none."
                    )
                ),
            ),
        ]

    def summary(self, config: dict) -> str:
        host = urlsplit(_base(config)).netloc
        tag = str(config.get("tag") or "").strip()
        return f"{host} · #{tag}" if tag else host

    def validate(self, config: dict) -> dict[str, list[str]]:
        """At the form: an address Postulo may dial, and not one that leaks the token.

        Every request this plugin makes carries ``Authorization: Token …``, so the scheme
        is not a matter of taste. Over ``http://`` to a host out on the internet the token
        is readable by anything on the way, and the person who typed the address is the
        only one in a position to fix it — which is why this is said here rather than at
        three in the morning when a document is archived (#2).

        Plain ``http://`` to the LAN is the self-hoster's normal case and stays allowed:
        ``approve_host`` resolves the name and holds it to the instance's own policy, so
        an address it comes back with is one Postulo would dial anyway, and whether that
        address is out on the internet is the whole of the question left.
        """
        url = str(config.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return {"url": [str(_("Give the whole address, beginning with https://."))]}
        try:
            address = approve_host(parts.hostname)
        except DestinationRefused as error:
            return {"url": [str(error)]}
        if parts.scheme == "http" and _is_public(address):
            return {
                "url": [
                    str(
                        _(
                            "This would send your API token unencrypted to a server on the "
                            "internet, where anything along the way can read it. Use https://."
                        )
                    )
                ]
            }
        return {}

    def test(self, config: dict) -> TestResult:
        try:
            with client(total=REQUEST_SECONDS) as session:
                count = Archive(config, client=session).count_documents()
        except (PaperlessError, ConnectionUnusable, httpx.HTTPError, DestinationRefused) as error:
            return TestResult(False, str(error))
        return TestResult(
            True,
            str(
                ngettext(
                    "Reachable. The archive holds %(count)d document.",
                    "Reachable. The archive holds %(count)d documents.",
                    count,
                )
            )
            % {"count": count},
        )

    def put(
        self, document, file, metadata: DocumentMetadata, config: dict, user
    ) -> ExternalRef | None:
        tag = str(config.get("tag") or "").strip()
        field_name = str(config.get("custom_field") or "").strip()
        checksum = md5_of(file)
        with client(total=UPLOAD_TIMEOUT) as session:
            archive = Archive(config, client=session)
            # Asked first (#27): a document an earlier attempt uploaded and Paperless consumed
            # after the wait, or one somebody archived by hand, is not sent a second time.
            document_id = archive.find_by_checksum(checksum) if checksum else None
            if document_id is None:
                fields = {
                    "title": compose_title(metadata)[:128],
                    "created": (metadata.sent_on or metadata.created_at.date()).isoformat(),
                    "document_type": archive.ensure("document_types", metadata.kind_label),
                }
                if metadata.company:
                    fields["correspondent"] = archive.ensure("correspondents", metadata.company)
                if tag:
                    fields["tags"] = [archive.ensure("tags", tag)]
                try:
                    task_id = archive.post_document(file, metadata, fields)
                except FileRefused as refusal:
                    logger.info("Paperless will not take %s: %s", metadata.filename, refusal)
                    return None
                document_id = archive.wait_for(task_id)
            # The document is archived from here on. The link back is a convenience: a failure
            # to write it must not make the copy look failed, or the whole file is uploaded
            # again for nothing, six times over (#12).
            if field_name and _is_absolute(metadata.application_url or ""):
                try:
                    field_id = archive.ensure("custom_fields", field_name, data_type="url")
                    archive.set_custom_field(document_id, field_id, metadata.application_url)
                except (PaperlessError, httpx.HTTPError) as error:
                    logger.warning(
                        "Document %s is archived in Paperless, but its link back to Postulo "
                        "could not be written: %s",
                        document_id,
                        _one_line(error),
                    )
        return ExternalRef(
            store=self.name,
            id=str(document_id),
            url=f"{archive.base}/documents/{document_id}/details",
        )
