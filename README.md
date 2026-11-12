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
- **Custom field** *Postulo* (URL) ← the application's address on your instance. Paperless
  only takes a whole address, so this needs `POSTULO_PUBLIC_URL` to be set on the Postulo
  instance; without it the field is left out. It is a convenience: if Paperless refuses it,
  the document is still recorded as archived.
- **Title** ← *CV — Company — Role — date* for what Postulo rendered; an upload keeps its
  own name, followed by the date.
- **Created** ← the day it was sent, or uploaded.

Before uploading, the plugin asks Paperless whether it already holds the file, by the MD5
checksum Paperless keeps of every original, and a document it already has (an earlier
attempt, or one you archived by hand) is not uploaded again. Paperless-ngx before 1.16
cannot be asked that, and is offered the file; it refuses a document whose checksum it
already holds and names the one it has, and the plugin reads that as success. Either way
a document is never archived twice, however many times it is offered — which is what makes
retrying safe. Consumption is asynchronous: the plugin waits up to five seconds for it, and
if Paperless is slower (OCR of a large scan, a busy server) it leaves the document to the
next scheduler pass, which finds it by its checksum.

The correspondents, document types, tag and custom field are looked up by name once and
remembered for five minutes, so a pass filing many documents for the same companies does
not ask again for each. One that was deleted in Paperless in the meantime is refused there
once, forgotten, and looked up again on the next attempt.

A file type Paperless has no parser for (a Word document without Tika, say) is offered once
and recorded as *not accepted*, not retried. A token Paperless no longer accepts (revoked, or
the account removed) switches the connection off with that reason instead of failing every
document in turn; enter a current token and switch it back on. Every answer is read with a
deadline and a size cap, and a redirect is never followed, so the token goes only to the
address you typed.

Nothing is ever deleted from Paperless. Withdrawing or declining an application changes
the timeline in Postulo and nothing in the archive; an archive is for keeping.

## Translations

French and Portuguese ship with the package, in `src/postulo_paperless/locale/`. Postulo never translates a plugin's strings, and nobody translates this one in a commit:
every translation is made in Weblate, at
[translate.tiagoagueda.com](https://translate.tiagoagueda.com/projects/postulo/), which drafts
new strings by machine and sends what speakers save back as a pull request from its
`weblate` branch. A commit runs `uv run postulo-messages extract`, which leaves an empty slot
for a new string in every catalogue, and `uv run postulo-messages guard` in CI refuses one
that writes a translation (postulo/postulo#706). The `.mo` files are not committed:
`uv run postulo-messages compile` writes them, before the tests and before `uv build`.
`uv run pytest -m release` holds the languages the paired core release selects complete before
a release.

A language code here is a BCP 47 tag in its canonical form, as everywhere in Postulo: the
language in lower case, a script with a capital, a region in capitals (`fr-FR`, `pt-BR`,
`sr-Cyrl`). The directories under `locale/` are the one exception: `fr_FR` and `sr_Cyrl`
are gettext's names for a catalogue, not codes, and `postulo-messages` derives them from
the tags, so nobody types one. Postulo hands a store each document's language as such a
tag (`DocumentMetadata.language`). This plugin sends nothing about it to Paperless today;
if it ever does, it sends the tag as it was handed over.

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
