# postulo-paperless

**Every document [Postulo](https://source.tiagoagueda.com/postulo/postulo) holds —
rendered CVs and letters, the files you uploaded — kept in a
[Paperless-ngx](https://docs.paperless-ngx.com/) archive, filed by company, kind and
application.**

Postulo keeps what you sent, exactly as it left, under its own private media. That stays
true: Postulo's copy is the source of truth, and nothing in Postulo depends on Paperless
being up. This plugin gives Paperless a *copy* of each document as it is created, so the
archive that already holds your contracts and letters holds your job search too, and a
document found there leads back to the application it was used for.

## Install

Into the environment of a running Postulo:

```sh
uv pip install git+https://source.tiagoagueda.com/postulo/postulo-paperless.git
```

Restart Postulo. In a container, bake it into the image — see *Plugins in the image* on
[Installing Postulo](https://source.tiagoagueda.com/postulo/postulo/wiki/Installing-Postulo):

```sh
docker compose -f docker/compose.yml build \
  --build-arg POSTULO_EXTRA_PACKAGES="git+https://source.tiagoagueda.com/postulo/postulo-paperless.git"
```

Paperless almost always sits on the same LAN or Compose network as Postulo. Postulo
refuses private addresses by default, so the operator sets
`POSTULO_CONNECTIONS_ALLOW_PRIVATE=true` for that; it is the reason that switch exists.

## Use

Under **Settings → Connections → Add a connection**, choose *Paperless-ngx*:

| Field | What it is |
| --- | --- |
| Paperless address | Where you open Paperless, e.g. `https://paperless.example.org` |
| API token | From your Paperless profile (*My profile → API auth token*). Stored encrypted, never shown again. |
| Tag | Put on every document from Postulo. Default `postulo`; blank for none. |
| Custom field for the application's link | A URL field leading back to the application. Default `Postulo`; blank for none. |

Then tick which kinds of document go to the archive — CV, cover letter, certificate,
portfolio, reference, other — and press *Test*: it lists the archive with your token.

From then on every new document is queued and the **scheduler** sends it on its next
pass. Each document shows *archived* with a link into Paperless, *waiting to be sent*,
or *failed* with the reason. *Send everything* on the connection queues what you already
had; *Send to stores now* under a document tries at once.

## What lands in Paperless

- **Correspondent** ← the company, created if missing.
- **Document type** ← the kind: *CV*, *Cover letter*, *Certificate*…
- **Tag** ← `postulo` (or yours).
- **Custom field** *Postulo* (URL) ← the application's address on your instance.
- **Title** ← *CV — Company — Role — date* for what Postulo rendered; an upload keeps its
  own name, followed by the date.
- **Created** ← the day it was sent, or uploaded.

Paperless refuses a document whose checksum it already holds and names the one it has;
the plugin reads that as success. So a document is never archived twice, however many
times it is offered — which is what makes retrying safe. Consumption is asynchronous:
the plugin waits a short while for it, and if Paperless is slower (OCR of a large scan,
a busy server) it leaves the document to the next scheduler pass, where the duplicate
answer picks it up.

Nothing is ever deleted from Paperless. Withdrawing or declining an application changes
the timeline in Postulo and nothing in the archive; an archive is for keeping.

## Translations

French and Portuguese ship with the package, in `src/postulo_paperless/locale/`. Postulo
never translates a plugin's strings; a catalogue for every language Postulo offers is in place; fill one and run
`uv run postulo-messages compile`. `uv run postulo-messages extract` refreshes them from
the source, and `uv run pytest -m release` holds the twenty-four European Union
catalogues complete before a release.

## Develop

```sh
uv sync        # brings in Postulo itself from its repository, for the tests
uv run pytest
uv run ruff check .
```

The tests run against a Paperless that lives in memory — its consumer, its task queue,
its duplicate check — and inside a real Postulo through its registry and scheduler. No
request leaves the machine.

## Licence

AGPL-3.0-or-later, like Postulo and like Paperless-ngx.
