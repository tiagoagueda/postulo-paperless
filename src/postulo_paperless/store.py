"""The store: post each document to Paperless-ngx, filed so it can be found again.

Paperless consumes a document asynchronously — ``POST /api/documents/post_document/``
answers with a task id, and the document exists once the task has run. ``put`` posts,
then waits a short while for the task; if Paperless is slower than that, it raises, and
Postulo's scheduler tries again later. That retry is safe because Paperless refuses a
document whose checksum it already holds and names the one it has, which this plugin
reads as success: a document is never archived twice, however many times it is offered.

What is filed, all created on the Paperless side when missing: the **correspondent** is
the company; the **document type** is the kind — CV, cover letter, certificate…; one
**tag** marks everything that came from Postulo; and a **custom field** holds the
application's address on the Postulo instance, so a document found in Paperless leads
back to where it was used. Withdrawing or declining an application deletes nothing here:
an archive is for keeping.

Every request goes through ``postulo.plugins.http.client()``, so the instance's
destination policy applies — a Paperless on the LAN needs the operator's
``POSTULO_CONNECTIONS_ALLOW_PRIVATE`` — and so do its timeouts and user agent.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

import httpx
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from postulo.plugins import http
from postulo.plugins.api import DocumentMetadata, ExternalRef
from postulo.plugins.base import FieldSpec, TestResult

from . import __version__

#: How long ``put`` waits for Paperless to consume a document before leaving it to the
#: next scheduler pass. Consumption usually takes a few seconds; OCR of a scan takes
#: longer, and a request must not hang for it.
WAIT_SECONDS = 20.0
POLL_SECONDS = 1.0
#: Uploading a document may take longer than the default request timeout.
UPLOAD_TIMEOUT = 60.0

_DUPLICATE_ID = re.compile(r"#(\d+)")


class PaperlessError(Exception):
    """Paperless answered, and the answer was no. The message is for the person."""


class StillConsuming(PaperlessError):
    """The task has not finished within the wait; the next pass will find the document."""


def _base(config: dict) -> str:
    return str(config.get("url", "")).rstrip("/")


def _headers(config: dict) -> dict:
    return {"Authorization": f"Token {config.get('token', '')}", "Accept": "application/json"}


def _explain(response: httpx.Response) -> str:
    detail = ""
    try:
        body = response.json()
        detail = body.get("detail", "") if isinstance(body, dict) else ""
    except ValueError:
        detail = response.text[:120]
    return f"HTTP {response.status_code}" + (f": {detail}" if detail else "")


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

    # ------------------------------------------------------------ plumbing

    def _get(self, path: str, **params) -> dict | list:
        response = self.client.get(f"{self.base}/api/{path}", params=params, headers=self.headers)
        if response.status_code != 200:
            raise PaperlessError(_explain(response))
        return response.json()

    def _post(self, path: str, **kwargs) -> httpx.Response:
        response = self.client.post(f"{self.base}/api/{path}", headers=self.headers, **kwargs)
        if response.status_code not in (200, 201):
            raise PaperlessError(_explain(response))
        return response

    # ---------------------------------------------------------- the calls

    def count_documents(self) -> int:
        return int(self._get("documents/", page_size=1)["count"])

    def ensure(self, kind: str, name: str, **extra) -> int:
        """The id of the correspondent, document type, tag or custom field called ``name``.

        Created when missing. Names are matched without regard to case, so "Black Mesa"
        and "black mesa" are the one correspondent.
        """
        found = self._get(f"{kind}/", name__iexact=name, page_size=1)
        results = found.get("results", []) if isinstance(found, dict) else []
        if results:
            return int(results[0]["id"])
        created = self._post(f"{kind}/", json={"name": name, **extra})
        return int(created.json()["id"])

    def post_document(self, file, metadata: DocumentMetadata, fields: dict) -> str:
        """Offer the file to the consumer. Returns the task id Paperless gave back."""
        data = {key: value for key, value in fields.items() if value not in (None, "", [])}
        response = self.client.post(
            f"{self.base}/api/documents/post_document/",
            headers=self.headers,
            data=data,
            files={"document": (metadata.filename, file, metadata.content_type)},
            timeout=UPLOAD_TIMEOUT,
        )
        if response.status_code != 200:
            raise PaperlessError(_explain(response))
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
                match = _DUPLICATE_ID.search(str((task or {}).get("result", "")))
                if match:
                    return int(match.group(1))
                raise PaperlessError(str(_("Paperless consumed the document but gave no id.")))
            if status == "FAILURE":
                result = str((task or {}).get("result", ""))
                match = _DUPLICATE_ID.search(result)
                if "duplicate" in result.lower() and match:
                    # Paperless already holds this exact file. That is the outcome we
                    # wanted, and it is why offering a document twice is harmless.
                    return int(match.group(1))
                raise PaperlessError(
                    str(_("Paperless could not consume the document: %(reason)s"))
                    % {"reason": result[:300] or status}
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
        response = self.client.patch(
            f"{self.base}/api/documents/{document_id}/",
            headers=self.headers,
            json={"custom_fields": fields},
        )
        if response.status_code != 200:
            raise PaperlessError(_explain(response))


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

    def test(self, config: dict) -> TestResult:
        try:
            with http.client() as client:
                count = Archive(config, client=client).count_documents()
        except (PaperlessError, httpx.HTTPError, http.DestinationRefused) as error:
            return TestResult(False, str(error))
        return TestResult(
            True,
            str(_("Reachable. The archive holds %(count)d documents.")) % {"count": count},
        )

    def put(self, document, file, metadata: DocumentMetadata, config: dict, user) -> ExternalRef:
        tag = str(config.get("tag") or "").strip()
        field_name = str(config.get("custom_field") or "").strip()
        with http.client() as client:
            archive = Archive(config, client=client)
            fields = {
                "title": compose_title(metadata)[:128],
                "created": (metadata.sent_on or metadata.created_at.date()).isoformat(),
                "document_type": archive.ensure("document_types", metadata.kind_label),
            }
            if metadata.company:
                fields["correspondent"] = archive.ensure("correspondents", metadata.company)
            if tag:
                fields["tags"] = [archive.ensure("tags", tag)]
            task_id = archive.post_document(file, metadata, fields)
            document_id = archive.wait_for(task_id)
            if field_name and metadata.application_url:
                field_id = archive.ensure("custom_fields", field_name, data_type="url")
                archive.set_custom_field(document_id, field_id, metadata.application_url)
        return ExternalRef(
            store=self.name,
            id=str(document_id),
            url=f"{archive.base}/documents/{document_id}/details",
        )
