# The API under `/api`

The hub has two interfaces. One is for the ESPuinos — `GET
/<espId>/card/<cardId>/manifest.json` and `GET /media/<path>` — and it is open,
because a device cannot log in. The other is the web UI, which the optional hub
password protects. This is a third one: the same hub, for programs that have no
browser and no session.

It exists because a tool that assigns cards has to write that assignment and
read what the write did. A program can post the HTML form, but its answer is a
redirect and a flash message — nothing a program can act on. This answers what
happened, in a shape a program can branch on.

## Auth

A shared secret, in the environment as `MEDIAHUB_API_SECRET` (see
`env-example`), carried on **every** call — reads included:

```
Authorization: Bearer <secret>
```

- **No secret configured means no API**: every `/api/` path answers `503`
  `no_api_secret`. That is the default, and deliberate: an unauthenticated
  write endpoint on a household LAN is worse than a missing one.
- **It is not the hub password.** The API is written for machines on the same
  network, so it stays reachable with the secret alone while the UI asks for a
  password. `GET /api/info` reports `ui_password_set` so a client can say so.
- A wrong or missing secret answers `401` (`bad_secret` / `missing_secret`).
  The secret is compared in constant time.

## Reads

### `GET /api/info`

What a client asks first: whether this hub is reachable with this secret at
all, and how new its interface is. A client should read `api` and refuse to
work with a hub that is older than it needs, rather than inferring the hub's
age from a 404.

```json
{"api": 1, "app": "ESPuino-Mediahub", "secret_required": true, "ui_password_set": false}
```

### `GET /api/devices`

The devices the hub knows and the card last placed on each. A device enters
this list by fetching a manifest, so one that was never used is absent; there
is no other way to register one. This is the list a client should take its
assignment targets from, rather than its own memory of them.

```json
[{"esp_id": "espuino-1", "alias": "Kitchen", "last_seen": "2026-11-18 20:31:49", "last_card_id": "123456789012"}]
```

`alias`, `last_seen` and `last_card_id` are `null` until the hub knows them.
The device's IP address is deliberately not part of this answer: it is the
admin UI's business, and a client acts on the other three.

### `GET /api/cards`

Every card the hub holds, optionally narrowed by query parameter:

- `?device=<espId>` — only that device's cards.
- `?status=<state>` — only cards the hub holds in that state. The value is
  compared against the card's own `status`; this API neither pins nor validates
  a vocabulary of states, it reports what the hub has recorded. A value no card
  carries simply selects nothing.

A device that holds no cards answers `[]`, and so does a mistyped device: the
API does not pretend to know which of the two a client meant. Taking the device
list from `/api/devices` is what tells them apart.

```json
[{"esp_id": "espuino-1", "card_id": "123456789012", "status": "assigned",
  "name": "Kitchen playlist", "kind": "files", "play_mode": 5, "stream_url": "",
  "play_root": "Album 01",
  "files": [{"path": "Album 01/track.mp3", "size": 1234567, "sha256": "…"}],
  "first_seen": "2026-11-18 20:31:49", "last_seen": "2026-11-18 20:31:49",
  "updated_at": "2026-11-18 20:32:10"}]
```

`card_id` is the card's own twelve-digit id, or `null` for a card the hub
registered before it had one. `files` is what the hub stores for the manifest
(`path`, `size`, `sha256`), which is also what a device will check its copy
against.

## The write

### `POST /api/cards/<espId>/<cardId>`

Assign content to one card on one device. The body is the assignment form's own
field names, `application/x-www-form-urlencoded`:

| field | |
|---|---|
| `name` | what the card is called |
| `kind` | `files` (default) or `webradio` |
| `play_mode` | a `manifest.FILE_PLAY_MODES` value — for `files` only |
| `selected_paths_json` | a JSON array of library-relative paths — for `files` only |
| `play_root` | the folder the selection was based on, optional |
| `stream_url` | the stream — for `webradio` only |

Validation is the assignment form's own (`_read_assignment` in `app.py`), not a
copy of it: the same play modes, the same dropped-file rule, the same "a
single-file mode takes exactly one path" and "a folder mode needs a folder the
selection shares". A rule that only the API enforced, or only the form,
would be a bug in whichever one an operator happened to use.

Two things are stricter here than the form, both deliberate:

- **The device has to exist** — `404` `unknown_device` otherwise. A form's
  ticked boxes are silently filtered to devices the hub knows; a client that
  mistypes a device id should be told, not have its write land nowhere.
- **`target_esp_ids` is refused** — `400` `unsupported_field`. The form writes
  one save to several devices at once; this API writes one device per call, so
  a client that means three devices posts three times. Ignoring the field would
  let it believe it had written three.

A card the hub has never seen is *created* by this write — that is the ordinary
case for a card a device has just reported, and no separate "add a card" step
is needed. `cardId` has to be twelve digits (`400` `invalid_card_id`),
which is also what keeps a caller from writing into the store under a key of
its own making.

The answer says what happened to every path that was sent:

```json
{"status": "assigned",
 "card": {…the card as /api/cards shows it…},
 "files": [{…}],
 "dropped": ["notes/readme.txt"],
 "warning": "Some selected files could not be used (missing, or not readable by the container's user) and were skipped: notes/readme.txt",
 "replaced": false}
```

- `files` is what the card now holds. **Fewer entries than were sent is the
  whole point of this shape**: "assigned, minus two unusable files" is a
  different outcome from a clean write, and a client that only saw `200` could
  not tell them apart.
- `dropped` names the paths that were sent and are not on the card.
- `warning` is the sentence the form would have flashed, when there is one.
- `replaced` says whether this write wrote over content the card already had.
  A repeated write is an **update**, never a duplicate — clients retry, and
  retrying must not stack cards up. But an update is also how an assignment a
  human made by hand gets clobbered, so the flag is there for a client to
  report rather than to hide. A card that was merely registered — tapped on a
  device, still empty — is *not* `replaced`: that is the ordinary case for a
  card that has just appeared.
- Status code: `201` when this write created the assignment, `200` when it
  replaced one.

## Errors

One shape everywhere, including for a path the hub refuses before running any
logic:

```json
{"error": "unknown_device", "message": "this hub has never seen a device 'espuino-7'"}
```

`error` is a word a program branches on, `message` a sentence a human acts on.
Both are always present; neither is ever prose to be parsed.

| code | word | |
|---|---|---|
| 400 | `invalid_card_id` | a card id is twelve digits |
| 400 | `unsupported_field` | `target_esp_ids` was sent |
| 400 | `unusable_input` | the hub's own validation refused; `message` says why |
| 401 | `missing_secret` / `bad_secret` | |
| 404 | `unknown_device` | |
| 503 | `no_api_secret` | no secret is configured on this hub |

The `message` of a `400 unusable_input` is the sentence the form would have
flashed, in the caller's language (`Accept-Language`), so an operator reading a
client's log meets the same words as in the UI. The API's own sentences are
not translated: they are read by programs and by whoever runs them.

## What it deliberately does not do

- **Nothing above a card.** The hub knows devices, cards and the assignments on
  them, and this surface reports exactly that. Planning, ordering, queueing,
  printing — anything a client tracks between *we want this* and *this card
  holds it* — is that client's own business, and nothing here asks about it or
  keeps any of it.
- **No delete.** Removing a card stays where it is, in the hub's own card view,
  because a delete may call `DELETE /rfid` on the device and that is not
  something a client should trigger as a side effect of tidying its own state.
- **No media browsing.** The library is a directory; a client that can read it
  reads it. The hub browses it for the UI, not for other programs.
- **No `verify`.** A card's stored sizes and hashes are what the manifest's
  version is built from. Nothing in this API changes a file, so nothing in it
  can make them stale; a tool that edits tags deliberately is the caller for
  that, and would be a separate endpoint.
- **No multi-device write.** One card, one device, per call (see above).

## Compatibility

This surface is additive, and the hub's existing users are untouched by it:

- The ESPuino-facing routes (`/<espId>/card/<cardId>/manifest.json`,
  `/media/<path>`, `/health`) are unchanged and stay open.
- The web UI and its JSON (`/cards/state`, `/media/browse*`) are unchanged; the
  assignment route keeps behaving exactly as before, with its validation now
  read through the same function the API uses.
- The stored data gains nothing: no new field, no migration. A card written
  through the API is the same record a card written through the form is.
- A hub with `MEDIAHUB_API_SECRET` unset behaves exactly like a hub before this
  existed, apart from answering `503` under `/api/`.

The API's own version is `1` (`GET /api/info`). Anything a client may come to
rely on beyond this document should raise that number.

## A worked example

```bash
export HUB=http://mediahub.local:8080
export SECRET=…            # the hub's MEDIAHUB_API_SECRET

curl -s -H "Authorization: Bearer $SECRET" $HUB/api/info
curl -s -H "Authorization: Bearer $SECRET" $HUB/api/devices

# assign two tracks of one folder, play mode 5 = "all tracks of a folder (sorted)"
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  --data-urlencode 'name=Kitchen playlist' \
  --data-urlencode 'kind=files' \
  --data-urlencode 'play_mode=5' \
  --data-urlencode 'play_root=Album 01' \
  --data-urlencode 'selected_paths_json=["Album 01/a.mp3","Album 01/b.mp3"]' \
  $HUB/api/cards/espuino-1/123456789012

# the device fetches its manifest and plays what was written
curl -s $HUB/espuino-1/card/123456789012/manifest.json | head -c 400
```
