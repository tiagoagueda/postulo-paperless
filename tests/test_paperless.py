"""The store, against a Paperless that lives in memory, and inside a real Postulo."""

from __future__ import annotations

import datetime as dt
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from django.core.files.base import ContentFile
from django.utils import timezone
from postulo.documents.archiving import send_pending
from postulo.documents.models import CopyStatus, DocumentKind, UploadedDocument
from postulo.plugins import registry
from postulo.plugins.api import DocumentMetadata, StorePlugin
from postulo.plugins.models import Connection

from postulo_paperless import store as store_module
from postulo_paperless.store import Archive, PaperlessError, PaperlessStore, compose_title

BASE = "https://paperless.example.org"
CONFIG = {"url": BASE + "/", "token": "tok3n", "tag": "postulo", "custom_field": "Postulo"}


class FakePaperless:
    """Enough of the Paperless-ngx API to file a document and find it again."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {
            "correspondents": [],
            "document_types": [],
            "tags": [],
            "custom_fields": [],
        }
        self.documents: dict[int, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.posted: list[dict] = []
        self.checksums: dict[bytes, int] = {}
        self.pending_polls = 0  # how many polls answer PENDING before SUCCESS
        self.reject_with: str | None = None
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        query = parse_qs(urlsplit(str(request.url)).query)
        self.requests.append((request.method, path))
        if request.headers.get("Authorization") != "Token tok3n":
            return httpx.Response(401, json={"detail": "Invalid token."})

        if path == "/api/documents/" and request.method == "GET":
            return httpx.Response(200, json={"count": len(self.documents), "results": []})

        for table in self.tables:
            if path == f"/api/{table}/":
                if request.method == "GET":
                    wanted = query.get("name__iexact", [""])[0].lower()
                    rows = [r for r in self.tables[table] if r["name"].lower() == wanted]
                    return httpx.Response(200, json={"count": len(rows), "results": rows})
                body = json.loads(request.content)
                row = {"id": len(self.tables[table]) + 1, **body}
                self.tables[table].append(row)
                return httpx.Response(201, json=row)

        if path == "/api/documents/post_document/":
            if self.reject_with:
                return httpx.Response(400, json={"detail": self.reject_with})
            content_type = request.headers["Content-Type"]
            assert content_type.startswith("multipart/form-data"), content_type
            body = request.content
            fields = _multipart_fields(body, content_type)
            self.posted.append(fields)
            file_bytes = fields["document"]["content"]
            task_id = f"task-{len(self.tasks) + 1}"
            if file_bytes in self.checksums:
                existing = self.checksums[file_bytes]
                title = self.documents[existing]["title"]
                self.tasks[task_id] = {
                    "status": "FAILURE",
                    "result": f"{fields['document']['filename']}: Not consuming: It is a "
                    f"duplicate of {title} (#{existing})",
                    "related_document": None,
                    "polls": 0,
                }
            else:
                document_id = len(self.documents) + 1
                self.documents[document_id] = {
                    "id": document_id,
                    "title": fields["title"],
                    "created": fields.get("created"),
                    "correspondent": fields.get("correspondent"),
                    "document_type": fields.get("document_type"),
                    "tags": [int(t) for t in fields.get("tags", [])] if "tags" in fields else [],
                    "custom_fields": [],
                }
                self.checksums[file_bytes] = document_id
                self.tasks[task_id] = {
                    "status": "SUCCESS",
                    "result": f"Success. New document id {document_id} created",
                    "related_document": str(document_id),
                    "polls": 0,
                }
            return httpx.Response(200, json=task_id)

        if path == "/api/tasks/":
            task = self.tasks[query["task_id"][0]]
            task["polls"] += 1
            if task["polls"] <= self.pending_polls:
                return httpx.Response(200, json=[{"status": "STARTED", "result": None}])
            return httpx.Response(200, json=[{"task_id": query["task_id"][0], **task}])

        if path.startswith("/api/documents/") and path.endswith("/"):
            document_id = int(path.split("/")[3])
            document = self.documents[document_id]
            if request.method == "GET":
                return httpx.Response(200, json=document)
            if request.method == "PATCH":
                document["custom_fields"] = json.loads(request.content)["custom_fields"]
                return httpx.Response(200, json=document)

        return httpx.Response(404, json={"detail": f"no route for {request.method} {path}"})


def _multipart_fields(body: bytes, content_type: str) -> dict:
    boundary = content_type.split("boundary=", 1)[1].encode()
    fields: dict = {}
    for part in body.split(b"--" + boundary)[1:-1]:
        head, _, content = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        content = content[: -len(b"\r\n")] if content.endswith(b"\r\n") else content
        headers = head.decode()
        name = headers.split('name="', 1)[1].split('"', 1)[0]
        if 'filename="' in headers:
            filename = headers.split('filename="', 1)[1].split('"', 1)[0]
            fields[name] = {"filename": filename, "content": content}
        elif name == "tags":
            fields.setdefault("tags", []).append(content.decode())
        else:
            fields[name] = content.decode()
    return fields


@pytest.fixture
def paperless(monkeypatch):
    fake = FakePaperless()

    def fake_client(**kwargs):
        kwargs.pop("event_hooks", None)
        kwargs.setdefault("timeout", 10)
        return httpx.Client(transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(store_module.http, "client", fake_client)
    monkeypatch.setattr(store_module, "POLL_SECONDS", 0)
    return fake


def a_metadata(**overrides) -> DocumentMetadata:
    values = {
        "kind": "cv",
        "kind_label": "CV",
        "origin": "render",
        "title": "Backend EN — CV",
        "filename": "backend-en.pdf",
        "content_type": "application/pdf",
        "created_at": dt.datetime(2026, 9, 6, 10, 30, tzinfo=dt.UTC),
        "checksum": "abc",
        "size": 12,
        "company": "Black Mesa",
        "role": "Research Engineer",
        "application_url": "https://postulo.example/applications/7/",
        "sent_on": dt.date(2026, 9, 6),
        "tags": ("postulo", "cv"),
    }
    values.update(overrides)
    return DocumentMetadata(**values)


# --------------------------------------------------------------------- titles


def test_the_title_reads_kind_company_role_date_for_a_render():
    assert compose_title(a_metadata()) == "CV — Black Mesa — Research Engineer — 2026-09-06"


def test_the_title_keeps_an_uploads_own_name_and_skips_what_is_missing():
    metadata = a_metadata(
        origin="upload", title="Diploma", company="", role="", application_url="", sent_on=None
    )
    assert compose_title(metadata) == "Diploma — 2026-09-06"


# ----------------------------------------------------------------- the store


def test_it_is_a_store_and_says_where_it_points():
    store = PaperlessStore()
    assert isinstance(store, StorePlugin)
    assert store.summary(CONFIG) == "paperless.example.org · #postulo"
    assert store.summary({**CONFIG, "tag": ""}) == "paperless.example.org"


def test_the_test_button_counts_documents_with_the_token(paperless):
    result = PaperlessStore().test(CONFIG)
    assert result.ok and result.message == "Reachable. The archive holds 0 documents."
    result = PaperlessStore().test({**CONFIG, "token": "wrong"})
    assert not result.ok and "HTTP 401: Invalid token." in result.message


def test_put_files_the_document_and_leads_back_to_the_application(paperless):
    ref = PaperlessStore().put(None, ContentFile(b"%PDF-1.7 cv"), a_metadata(), CONFIG, None)
    assert ref.store == "paperless" and ref.id == "1"
    assert ref.url == f"{BASE}/documents/1/details"

    posted = paperless.posted[0]
    assert posted["title"] == "CV — Black Mesa — Research Engineer — 2026-09-06"
    assert posted["created"] == "2026-09-06"
    assert posted["document"] == {"filename": "backend-en.pdf", "content": b"%PDF-1.7 cv"}
    assert paperless.tables["correspondents"] == [{"id": 1, "name": "Black Mesa"}]
    assert paperless.tables["document_types"] == [{"id": 1, "name": "CV"}]
    assert paperless.tables["tags"] == [{"id": 1, "name": "postulo"}]
    assert posted["correspondent"] == "1" and posted["document_type"] == "1"
    assert posted["tags"] == ["1"]
    assert paperless.tables["custom_fields"] == [{"id": 1, "name": "Postulo", "data_type": "url"}]
    assert paperless.documents[1]["custom_fields"] == [
        {"field": 1, "value": "https://postulo.example/applications/7/"}
    ]


def test_names_are_reused_rather_than_created_twice(paperless):
    store = PaperlessStore()
    store.put(None, ContentFile(b"one"), a_metadata(), CONFIG, None)
    store.put(None, ContentFile(b"two"), a_metadata(company="black mesa"), CONFIG, None)
    assert len(paperless.tables["correspondents"]) == 1
    assert len(paperless.tables["document_types"]) == 1
    assert len(paperless.tables["tags"]) == 1
    assert len(paperless.tables["custom_fields"]) == 1
    assert len(paperless.documents) == 2


def test_offering_the_same_file_again_finds_the_one_paperless_already_has(paperless):
    store = PaperlessStore()
    first = store.put(None, ContentFile(b"same bytes"), a_metadata(), CONFIG, None)
    again = store.put(None, ContentFile(b"same bytes"), a_metadata(), CONFIG, None)
    assert first.id == again.id == "1"
    assert len(paperless.documents) == 1, "a duplicate answer is success, not a second copy"


def test_a_slow_consumer_is_left_to_the_next_pass(paperless, monkeypatch):
    paperless.pending_polls = 1000
    monkeypatch.setattr(store_module, "WAIT_SECONDS", 0.0)
    with pytest.raises(PaperlessError, match="still consuming"):
        PaperlessStore().put(None, ContentFile(b"slow"), a_metadata(), CONFIG, None)


def test_a_consumer_that_needs_a_few_polls_is_waited_for(paperless):
    paperless.pending_polls = 3
    ref = PaperlessStore().put(None, ContentFile(b"ok"), a_metadata(), CONFIG, None)
    assert ref.id == "1"
    assert paperless.tasks["task-1"]["polls"] == 4


def test_a_refusal_is_explained(paperless):
    paperless.reject_with = "File type text/plain not supported"
    with pytest.raises(PaperlessError, match="HTTP 400: File type text/plain not supported"):
        PaperlessStore().put(None, ContentFile(b"x"), a_metadata(), CONFIG, None)


def test_blank_tag_and_field_mean_none(paperless):
    config = {**CONFIG, "tag": "", "custom_field": ""}
    PaperlessStore().put(None, ContentFile(b"x"), a_metadata(), config, None)
    assert paperless.tables["tags"] == [] and paperless.tables["custom_fields"] == []
    assert "tags" not in paperless.posted[0]


def test_an_existing_custom_field_on_the_document_is_kept(paperless):
    store = PaperlessStore()
    store.put(None, ContentFile(b"x"), a_metadata(), CONFIG, None)
    paperless.documents[1]["custom_fields"].append({"field": 9, "value": "kept"})
    store.put(None, ContentFile(b"x"), a_metadata(application_url="https://p/a/8/"), CONFIG, None)
    assert paperless.documents[1]["custom_fields"] == [
        {"field": 9, "value": "kept"},
        {"field": 1, "value": "https://p/a/8/"},
    ]


def test_the_archive_helper_explains_non_json_errors():
    def handler(request):
        return httpx.Response(502, text="<html>bad gateway</html>")

    archive = Archive(CONFIG, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PaperlessError, match="HTTP 502"):
        archive.count_documents()


# --------------------------------------------------------------- in Postulo


@pytest.mark.django_db
def test_postulo_finds_the_store_and_the_scheduler_sends_through_it(paperless, settings):
    from django.contrib.auth import get_user_model

    registry.plugins("store", refresh=True)
    assert registry.find_plugin("store", "paperless") is not None

    user = get_user_model().objects.create_user(email="ana@example.org", password="pw" * 8)  # noqa: S106
    connection = Connection(
        owner=user,
        kind="store",
        plugin="paperless",
        label="Home",
        config={"url": BASE, "tag": "postulo", "custom_field": "Postulo"},
    )
    connection.secrets = {"token": "tok3n"}
    connection.save()

    upload = UploadedDocument(owner=user, title="Diploma", kind=DocumentKind.CERTIFICATE)
    upload.file.save("diploma.pdf", ContentFile(b"%PDF-1.7 diploma"), save=False)
    upload.save()
    copy = upload.copies.get()
    assert copy.status == CopyStatus.PENDING

    assert send_pending() == (1, 0)
    copy.refresh_from_db()
    assert copy.status == CopyStatus.SENT and copy.external_id == "1"
    assert copy.external_url == f"{BASE}/documents/1/details"
    assert paperless.documents[1]["title"] == f"Diploma — {timezone.now().date().isoformat()}"
    assert paperless.tables["document_types"] == [{"id": 1, "name": "Certificate"}]
    assert paperless.tables["correspondents"] == [], "an upload has no company"


@pytest.mark.django_db
def test_the_plugin_speaks_french_and_portuguese_by_itself():
    from django.utils import translation

    registry.plugins("store", refresh=True)
    store = registry.find_plugin("store", "paperless")
    with translation.override("fr-fr"):
        assert store.config_fields()[0].label == "Adresse de Paperless"
    with translation.override("pt-pt"):
        assert store.config_fields()[0].label == "Endereço do Paperless"
    with translation.override("en-gb"):
        assert store.config_fields()[0].label == "Paperless address"
