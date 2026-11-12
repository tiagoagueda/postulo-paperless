"""The store, against a Paperless that lives in memory, and inside a real Postulo."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from django.core.files.base import ContentFile
from django.utils import timezone
from postulo.documents.archiving import send_pending
from postulo.documents.models import CopyStatus, DocumentKind, UploadedDocument
from postulo.plugins import registry
from postulo.plugins.api import (
    ConnectionUnusable,
    DestinationRefused,
    DocumentMetadata,
    StorePlugin,
)
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
        #: Each document's MD5, as Paperless keeps it and its metadata names it.
        self.md5s: dict[int, str] = {}
        #: Whether ``checksum__iexact`` is understood. Paperless-ngx before 1.16 ignores a
        #: filter it does not know and answers with every document.
        self.checksum_filter = True
        self.pending_polls = 0  # how many polls answer PENDING before SUCCESS
        #: The refusal of the file, as Paperless words it: under the ``document`` field, in a list.
        self.reject_with: str | None = None
        #: When set, the answer to the PATCH that writes the custom field.
        self.patch_with: httpx.Response | None = None
        self.requests: list[tuple[str, str]] = []
        #: What ``client()`` was asked for, to show the deadline is handed on.
        self.client_calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        query = parse_qs(urlsplit(str(request.url)).query)
        self.requests.append((request.method, path))
        if request.headers.get("Authorization") != "Token tok3n":
            return httpx.Response(401, json={"detail": "Invalid token."})

        if path == "/api/documents/" and request.method == "GET":
            wanted = query.get("checksum__iexact", [""])[0].lower()
            if wanted and self.checksum_filter:
                rows = [{"id": i} for i, md5 in self.md5s.items() if md5 == wanted]
            else:
                rows = [{"id": i} for i in self.documents]
            return httpx.Response(200, json={"count": len(rows), "results": rows[:1]})

        if path.startswith("/api/documents/") and path.endswith("/metadata/"):
            document_id = int(path.split("/")[3])
            return httpx.Response(200, json={"original_checksum": self.md5s[document_id]})

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
                return httpx.Response(400, json={"document": [self.reject_with]})
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
                name = fields["document"]["filename"]
                # Paperless-ngx's own wording: the file name twice, the title before the id,
                # a full stop at the end.
                self.tasks[task_id] = {
                    "status": "FAILURE",
                    "result": f"{name}: Not consuming {name}: It is a duplicate of {title} "
                    f"(#{existing}).",
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
                self.md5s[document_id] = hashlib.md5(file_bytes, usedforsecurity=False).hexdigest()
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
                if self.patch_with is not None:
                    return self.patch_with
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
        fake.client_calls.append(dict(kwargs))
        kwargs.pop("total", None)  # the real client holds each request to it; a mock cannot
        kwargs.setdefault("timeout", 10)
        return httpx.Client(transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(store_module, "client", fake_client)
    monkeypatch.setattr(store_module, "POLL_SECONDS", 0)
    return fake


@pytest.fixture(autouse=True)
def _a_new_process():
    """Every test starts with nothing remembered, as a new process does (#27)."""
    store_module.forget_lookups()
    yield
    store_module.forget_lookups()


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


def test_the_version_postulo_shows_is_the_version_that_was_installed():
    """Not a string somebody has to remember to change: it said 0.1.0 at 0.3.0 (#2)."""
    declared = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    assert PaperlessStore().version == declared


def test_it_is_a_store_and_says_where_it_points():
    store = PaperlessStore()
    assert isinstance(store, StorePlugin)
    assert store.summary(CONFIG) == "paperless.example.org · #postulo"
    assert store.summary({**CONFIG, "tag": ""}) == "paperless.example.org"


# --------------------------------------------------------- at the form

# The addresses `approve_host` would hand back for the hosts these tests type, so that
# nothing here depends on what a name resolves to on the machine running the suite.
RESOLVES = {
    "paperless.example.org": "93.184.216.34",
    "paperless.lan": "192.168.1.20",
    "127.0.0.1": "127.0.0.1",
}


@pytest.fixture
def resolving(monkeypatch):
    def approve_host(host: str) -> str:
        if host not in RESOLVES:
            raise DestinationRefused(f"{host} is not an address this instance may reach.")
        return RESOLVES[host]

    monkeypatch.setattr(store_module, "approve_host", approve_host)


def test_https_to_a_public_archive_is_accepted(resolving):
    assert PaperlessStore().validate(CONFIG) == {}


def test_plain_http_to_the_internet_is_refused_because_of_the_token(resolving):
    problems = PaperlessStore().validate({**CONFIG, "url": "http://paperless.example.org/"})
    assert "unencrypted" in problems["url"][0]


def test_plain_http_on_ones_own_network_is_how_self_hosting_works(resolving):
    assert PaperlessStore().validate({**CONFIG, "url": "http://paperless.lan:8000/"}) == {}
    assert PaperlessStore().validate({**CONFIG, "url": "http://127.0.0.1:8000/"}) == {}


def test_an_address_the_instance_will_not_dial_is_said_at_the_form(resolving):
    problems = PaperlessStore().validate({**CONFIG, "url": "https://paperless.invalid/"})
    assert "may reach" in problems["url"][0]


@pytest.mark.parametrize("url", ["", "paperless.example.org", "ftp://paperless.example.org"])
def test_something_that_is_not_an_address_is_refused_before_anything_is_resolved(url):
    assert PaperlessStore().validate({**CONFIG, "url": url}) == {
        "url": ["Give the whole address, beginning with https://."]
    }


def test_the_test_button_counts_documents_with_the_token(paperless):
    result = PaperlessStore().test(CONFIG)
    assert result.ok and result.message == "Reachable. The archive holds 0 documents."
    result = PaperlessStore().test({**CONFIG, "token": "wrong"})
    assert not result.ok and "HTTP 401: Invalid token." in result.message


def test_the_test_button_says_one_document_in_the_singular(paperless):
    paperless.documents[1] = {"id": 1, "title": "x", "custom_fields": []}
    result = PaperlessStore().test(CONFIG)
    assert result.ok and result.message == "Reachable. The archive holds 1 document."
    paperless.documents[2] = {"id": 2, "title": "y", "custom_fields": []}
    assert PaperlessStore().test(CONFIG).message == "Reachable. The archive holds 2 documents."


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


# ------------------------------------------- fewer requests and uploads (#27)


def _asked(paperless, method: str, path: str) -> int:
    return sum(1 for request in paperless.requests if request == (method, path))


NAME_LOOKUPS = ("/api/document_types/", "/api/correspondents/", "/api/tags/", "/api/custom_fields/")


def test_a_batch_for_one_company_looks_each_name_up_once(paperless):
    store = PaperlessStore()
    store.put(None, ContentFile(b"first"), a_metadata(), CONFIG, None)
    asked = {path: _asked(paperless, "GET", path) for path in NAME_LOOKUPS}
    assert asked == dict.fromkeys(NAME_LOOKUPS, 1)
    for number in range(4):
        content = ContentFile(f"letter {number}".encode())
        store.put(None, content, a_metadata(company="black mesa"), CONFIG, None)
    assert {path: _asked(paperless, "GET", path) for path in NAME_LOOKUPS} == asked
    assert len(paperless.documents) == 5
    assert {doc["correspondent"] for doc in paperless.documents.values()} == {"1"}


def test_a_name_is_looked_up_again_once_remembered_long_enough(paperless, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(store_module.time, "monotonic", lambda: clock[0])
    store = PaperlessStore()
    store.put(None, ContentFile(b"one"), a_metadata(), CONFIG, None)
    clock[0] += store_module.LOOKUP_SECONDS - 1
    store.put(None, ContentFile(b"two"), a_metadata(), CONFIG, None)
    assert _asked(paperless, "GET", "/api/correspondents/") == 1
    clock[0] += 1
    store.put(None, ContentFile(b"three"), a_metadata(), CONFIG, None)
    assert _asked(paperless, "GET", "/api/correspondents/") == 2


def test_an_id_paperless_refuses_is_forgotten_and_asked_for_again(paperless):
    store = PaperlessStore()
    store.put(None, ContentFile(b"one"), a_metadata(), CONFIG, None)
    # Somebody deletes the correspondent in Paperless and makes it again, with a new id.
    paperless.tables["correspondents"] = [{"id": 7, "name": "Black Mesa"}]
    handler = paperless.handler

    def refusing_the_old_id(request):
        if (
            request.url.path == "/api/documents/post_document/"
            and b'name="correspondent"\r\n\r\n1\r\n' in request.content
        ):
            paperless.requests.append((request.method, request.url.path))
            return httpx.Response(
                400, json={"correspondent": ['Invalid pk "1" - object does not exist.']}
            )
        return handler(request)

    paperless.handler = refusing_the_old_id
    with pytest.raises(PaperlessError, match="Invalid pk"):
        store.put(None, ContentFile(b"two"), a_metadata(), CONFIG, None)
    assert _asked(paperless, "GET", "/api/correspondents/") == 1, "the refused id was remembered"
    ref = store.put(None, ContentFile(b"two"), a_metadata(), CONFIG, None)
    assert _asked(paperless, "GET", "/api/correspondents/") == 2
    assert _asked(paperless, "GET", "/api/document_types/") == 1, "the others are still good"
    assert paperless.documents[int(ref.id)]["correspondent"] == "7"


def test_a_retry_after_a_slow_consumption_does_not_upload_again(paperless, monkeypatch):
    """The first attempt gives up waiting; Paperless finishes; the retry only asks."""
    paperless.pending_polls = 1000
    monkeypatch.setattr(store_module, "WAIT_SECONDS", 0.0)
    store = PaperlessStore()
    with pytest.raises(PaperlessError, match="still consuming"):
        store.put(None, ContentFile(b"a long scan"), a_metadata(), CONFIG, None)
    ref = store.put(None, ContentFile(b"a long scan"), a_metadata(), CONFIG, None)
    assert ref.id == "1"
    assert _asked(paperless, "POST", "/api/documents/post_document/") == 1
    assert _asked(paperless, "GET", "/api/tasks/") == 1, "nothing to wait for the second time"


def test_a_document_archived_by_hand_is_not_uploaded(paperless):
    store = PaperlessStore()
    store.put(None, ContentFile(b"by hand"), a_metadata(), CONFIG, None)
    paperless.requests.clear()
    ref = store.put(None, ContentFile(b"by hand"), a_metadata(), CONFIG, None)
    assert ref.id == "1"
    assert _asked(paperless, "POST", "/api/documents/post_document/") == 0
    assert not [r for r in paperless.requests if r[1] in NAME_LOOKUPS and r[0] == "GET"]


def test_a_paperless_that_ignores_the_filter_is_offered_the_file(paperless):
    """Before 1.16 the filter is ignored and every document comes back: none is taken for it."""
    paperless.checksum_filter = False
    store = PaperlessStore()
    store.put(None, ContentFile(b"the only one"), a_metadata(), CONFIG, None)
    ref = store.put(None, ContentFile(b"another"), a_metadata(), CONFIG, None)
    assert ref.id == "2", "one document in the archive is not a match for every file"
    assert _asked(paperless, "POST", "/api/documents/post_document/") == 2


def test_a_paperless_that_refuses_the_filter_is_offered_the_file(paperless):
    handler = paperless.handler

    def refusing(request):
        if request.url.path == "/api/documents/" and "checksum__iexact" in str(request.url):
            return httpx.Response(400, json={"checksum__iexact": ["Unknown filter."]})
        return handler(request)

    paperless.handler = refusing
    ref = PaperlessStore().put(None, ContentFile(b"x"), a_metadata(), CONFIG, None)
    assert ref.id == "1" and len(paperless.posted) == 1


def test_the_wait_is_short_because_the_retry_costs_one_question():
    assert store_module.WAIT_SECONDS <= 5


def test_a_consumer_that_needs_a_few_polls_is_waited_for(paperless):
    paperless.pending_polls = 3
    ref = PaperlessStore().put(None, ContentFile(b"ok"), a_metadata(), CONFIG, None)
    assert ref.id == "1"
    assert paperless.tasks["task-1"]["polls"] == 4


def test_a_refusal_is_explained(paperless):
    """Paperless puts a validation refusal under the field's name, in a list (#14)."""
    response = httpx.Response(400, json={"document": ["File type text/plain not supported"]})
    assert (
        store_module._explain(response) == "HTTP 400: document: File type text/plain not supported"
    )
    other = httpx.Response(400, json={"tags": ["Invalid pk"], "title": ["Too long.", "Bad."]})
    assert store_module._explain(other) == "HTTP 400: tags: Invalid pk title: Too long. title: Bad."
    assert store_module._explain(httpx.Response(400, json=["one", "two"])) == "HTTP 400: one two"


def test_a_file_paperless_will_not_consume_is_declined_not_retried(paperless):
    paperless.reject_with = "File type text/plain not supported"
    ref = PaperlessStore().put(None, ContentFile(b"x"), a_metadata(), CONFIG, None)
    assert ref is None, "the store contract's `not for me`"
    uploads = [r for r in paperless.requests if r == ("POST", "/api/documents/post_document/")]
    assert len(uploads) == 1


def test_a_refusal_on_another_field_is_a_failure_that_names_it(paperless, monkeypatch):
    original = paperless.handler

    def handler(request):
        if request.url.path == "/api/documents/post_document/":
            return httpx.Response(400, json={"created": ["Date has wrong format."]})
        return original(request)

    monkeypatch.setattr(paperless, "handler", handler)
    with pytest.raises(PaperlessError, match="HTTP 400: created: Date has wrong format."):
        PaperlessStore().put(None, ContentFile(b"x"), a_metadata(), CONFIG, None)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"detail": "first line\nforged: log line\r\n" + "x" * 1000}),
        httpx.Response(400, json={"document": ["first line\nforged log line " + "x" * 1000]}),
        httpx.Response(502, text="first line\nforged log line " + "x" * 1000),
    ],
)
def test_what_the_server_says_is_one_line_of_at_most_a_hundred_and_twenty(response):
    message = store_module._explain(response)
    assert "\n" not in message and "\r" not in message
    assert len(message) <= len(f"HTTP {response.status_code}: ") + 120
    assert "first line" in message


def test_a_failed_task_reason_is_one_line_too(paperless):
    archive = Archive(CONFIG, client=httpx.Client(transport=httpx.MockTransport(paperless.handler)))
    paperless.tasks["t"] = {"status": "FAILURE", "result": "bad\nforged line", "polls": 0}
    with pytest.raises(PaperlessError) as raised:
        archive.wait_for("t")
    assert "\n" not in str(raised.value) and "bad forged line" in str(raised.value)


# ------------------------------------------------------------ a revoked token (#13)


def test_a_wrong_token_retires_the_connection_and_the_button_still_answers(paperless):
    wrong = {**CONFIG, "token": "revoked"}
    with pytest.raises(ConnectionUnusable, match="no longer accepts the API token") as raised:
        PaperlessStore().put(None, ContentFile(b"x"), a_metadata(), wrong, None)
    assert "Invalid token." in str(raised.value)
    assert raised.value.keep_secrets is False, "a revoked token is not worth keeping"
    result = PaperlessStore().test(wrong)
    assert not result.ok and "Invalid token." in result.message


def test_forbidden_is_an_ordinary_failure_because_one_permission_is_missing(paperless):
    def handler(request):
        return httpx.Response(403, json={"detail": "You do not have permission."})

    archive = Archive(CONFIG, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PaperlessError, match="HTTP 403") as raised:
        archive.count_documents()
    assert not isinstance(raised.value, ConnectionUnusable)


# ------------------------------------------------------- the duplicate's id (#11)


def test_a_retried_document_is_matched_by_the_id_at_the_end_of_the_message(paperless):
    paperless.checksum_filter = False  # a Paperless older than 1.16 is offered the file again
    store = PaperlessStore()
    store.put(None, ContentFile(b"someone else's"), a_metadata(role="Other"), CONFIG, None)
    first = store.put(
        None, ContentFile(b"mine"), a_metadata(role="Engineer (Req #1)"), CONFIG, None
    )
    assert first.id == "2"
    untouched = json.loads(json.dumps(paperless.documents[1]["custom_fields"]))
    again = store.put(
        None, ContentFile(b"mine"), a_metadata(role="Engineer (Req #1)"), CONFIG, None
    )
    assert again.id == "2", "Paperless named document 2, not the `#1` in its title"
    assert paperless.documents[1]["custom_fields"] == untouched
    assert len(paperless.documents) == 2


def test_an_uploads_own_title_with_a_hash_does_not_win_either(paperless):
    paperless.checksum_filter = False
    store = PaperlessStore()
    upload = a_metadata(origin="upload", title="Ticket #48213 — scan", company="", role="")
    store.put(None, ContentFile(b"filler"), a_metadata(), CONFIG, None)
    first = store.put(None, ContentFile(b"scan"), upload, CONFIG, None)
    again = store.put(None, ContentFile(b"scan"), upload, CONFIG, None)
    assert first.id == again.id == "2"
    assert "(#2)." in paperless.tasks["task-3"]["result"], "Paperless's full wording"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("a.pdf: Not consuming a.pdf: It is a duplicate of CV (#9) (#57).", 57),
        ("It is a duplicate of Req #12 (#5). Note: existing document is in the trash.", 5),
        ("duplicate of Req #12 and #34", 34),
        ("nothing here", None),
    ],
)
def test_the_duplicates_id_is_the_last_one(result, expected):
    assert store_module._duplicate_id(result) == expected


# ------------------------------------------------------ the link back (#12)


def test_a_bare_path_is_not_written_into_a_url_field(paperless):
    metadata = a_metadata(application_url="/applications/7/")
    ref = PaperlessStore().put(None, ContentFile(b"x"), metadata, CONFIG, None)
    assert ref.id == "1"
    assert not [r for r in paperless.requests if r[0] == "PATCH"]
    assert paperless.tables["custom_fields"] == [], "the field is not even created"


def test_a_refused_link_does_not_make_an_archived_document_a_failure(paperless, caplog):
    paperless.patch_with = httpx.Response(400, json={"custom_fields": ["Enter a valid URL."]})
    with caplog.at_level("WARNING"):
        ref = PaperlessStore().put(None, ContentFile(b"x"), a_metadata(), CONFIG, None)
    assert ref.id == "1" and ref.url == f"{BASE}/documents/1/details"
    assert "custom_fields: Enter a valid URL." in caplog.text
    assert "tok3n" not in caplog.text


# ------------------------------------------------- bounded answers (#16)


def test_every_request_runs_under_a_deadline_of_its_own(paperless):
    PaperlessStore().test(CONFIG)
    PaperlessStore().put(None, ContentFile(b"x"), a_metadata(), CONFIG, None)
    assert [call["total"] for call in paperless.client_calls] == [
        store_module.REQUEST_SECONDS,
        store_module.UPLOAD_TIMEOUT,
    ]


def test_an_answer_over_the_cap_is_refused_unread():
    def declared(request):
        return httpx.Response(200, content=b"x" * (store_module.MAX_BODY + 1))

    class Endless(httpx.SyncByteStream):
        def __iter__(self):
            while True:
                yield b"x" * 65536

    def undeclared(request):
        return httpx.Response(200, stream=Endless())

    for handler in (declared, undeclared):
        archive = Archive(CONFIG, client=httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(PaperlessError, match="more than this plugin reads"):
            archive.count_documents()


def test_a_server_answering_a_byte_at_a_time_is_cut_at_the_deadline(monkeypatch):
    import time

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            while True:
                time.sleep(0.05)
                yield b" "

    def handler(request):
        return httpx.Response(200, stream=Trickle())

    monkeypatch.setattr(store_module, "REQUEST_SECONDS", 0.3)
    archive = Archive(CONFIG, client=httpx.Client(transport=httpx.MockTransport(handler)))
    started = time.monotonic()
    with pytest.raises(PaperlessError, match="too slowly"):
        archive.count_documents()
    assert time.monotonic() - started < 3


def test_a_redirect_is_not_followed_and_the_token_goes_nowhere_else():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "paperless.example.org":
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/api/"})
        return httpx.Response(200, json={"count": 0, "results": []})

    archive = Archive(CONFIG, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PaperlessError, match="HTTP 302"):
        archive.count_documents()
    assert all("elsewhere" not in url for url in seen)


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


def _a_connection_with_two_pending_copies(token: str):
    from django.contrib.auth import get_user_model

    registry.plugins("store", refresh=True)
    user = get_user_model().objects.create_user(email="ana@example.org", password="pw" * 8)  # noqa: S106
    connection = Connection(
        owner=user,
        kind="store",
        plugin="paperless",
        label="Home",
        config={"url": BASE, "tag": "postulo", "custom_field": "Postulo"},
    )
    connection.secrets = {"token": token}
    connection.save()
    for name in ("one", "two"):
        upload = UploadedDocument(owner=user, title=name, kind=DocumentKind.CERTIFICATE)
        upload.file.save(f"{name}.pdf", ContentFile(b"%PDF-1.7 " + name.encode()), save=False)
        upload.save()
    return connection


@pytest.mark.django_db
def test_a_dead_token_switches_the_connection_off_once_through_the_scheduler(paperless):
    connection = _a_connection_with_two_pending_copies("revoked")
    send_pending()
    connection.refresh_from_db()
    assert not connection.enabled
    assert "no longer accepts the API token" in connection.last_error
    assert connection.secrets == {}, "a token known not to work is not kept"
    # The pass that found the token dead read both copies with the connection still on, so the
    # second is tried once more in it (the core's doing); no later pass dials it again.
    dialled = len(paperless.requests)
    send_pending()
    assert len(paperless.requests) == dialled, "a switched-off connection is not dialled"


@pytest.mark.django_db
def test_a_file_paperless_cannot_consume_ends_declined_after_one_upload(paperless):
    from postulo.documents.models import DocumentCopy

    _a_connection_with_two_pending_copies("tok3n")
    paperless.reject_with = "File type application/msword not supported"
    for _attempt in range(3):
        send_pending()
    statuses = set(DocumentCopy.objects.values_list("status", flat=True))
    assert statuses == {CopyStatus.DECLINED}
    uploads = [r for r in paperless.requests if r[1] == "/api/documents/post_document/"]
    assert len(uploads) == 2, "one upload per file, not six"


@pytest.mark.django_db
def test_the_plugin_speaks_french_and_portuguese_by_itself():
    from django.utils import translation

    registry.plugins("store", refresh=True)
    store = registry.find_plugin("store", "paperless")
    with translation.override("fr-FR"):
        assert store.config_fields()[0].label == "Adresse de Paperless"
    with translation.override("pt-PT"):
        assert store.config_fields()[0].label == "Endereço do Paperless"
    with translation.override("en-GB"):
        assert store.config_fields()[0].label == "Paperless address"
