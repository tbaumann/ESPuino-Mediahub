"""The hub's API: the same hub, driven by a program instead of a browser.

Everything the admin UI does is also done here, for clients that have no browser
and no session — a tool that assigns cards on its own has to write that
assignment and read what the write did, and driving the HTML form for that is
both fragile and impossible to read the result of.

The surface is small on purpose and it adds no rules of its own. Reads come
straight from the store. The write goes through the same two calls the web form
ends in — `_read_assignment` (the validation) and `store.save_assignment` (the
write) — because a rule only one of the two enforced would be a bug in
whichever one an operator happened to use. The single place this API is
stricter than the form is deliberate: a device the hub has never seen is
refused, where an unticked box in a form is silently dropped.

It knows nothing the hub itself does not know: devices, cards and their
assignments. Whatever a client does above that — plan, order, queue, print —
is the client's own business and none of this surface's.

Auth is a shared secret (`MEDIAHUB_API_SECRET`), carried as
`Authorization: Bearer <secret>` on reads as well as writes. It is not the
admin password: this API is written for machines on the same network, so it
stays reachable with the secret alone even when the UI password is set. A hub
without a secret serves no API at all, because an unauthenticated write
endpoint is worse than a missing one.

The surface itself, and what a client may expect of it, is written down in
`docs/api.md`.
"""

import hmac
import json
import os

from flask import Blueprint, jsonify, request

import manifest as manifest_lib

#: The version of this interface, answered by `/api/info` — so a client can ask
#: how new the hub's API is instead of inferring it from a 404.
API_VERSION = 1

#: Where the shared secret comes from. The hub's other settings are environment
#: variables too (see env-example), and a secret belongs in the deployment
#: rather than in the database next to the data it protects.
SECRET_ENV = "MEDIAHUB_API_SECRET"


def build(store, read_assignment):
    """The `/api/` blueprint.

    Both dependencies are the hub's own and are handed in rather than imported,
    so this module stays a description of a surface: `store` is the running
    hub's store, and `read_assignment` is the validation the web form uses.
    """
    api = Blueprint("api", __name__, url_prefix="/api")

    # -- auth -------------------------------------------------------------
    @api.before_request
    def _require_secret():
        secret = _configured_secret()
        if not secret:
            return _error(
                503,
                "no_api_secret",
                f"this hub serves no API: set {SECRET_ENV} in its environment and restart it",
            )
        header = request.headers.get("Authorization", "")
        if not header:
            return _error(401, "missing_secret", "the Authorization header is missing")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), secret):
            return _error(
                401,
                "bad_secret",
                "the Authorization header must read 'Bearer <secret>', with this hub's secret",
            )

    # -- reads ------------------------------------------------------------
    @api.get("/info")
    def info():
        """What a client asks first: whether this hub is reachable with this
        secret at all, and how new its interface is."""
        return jsonify(
            api=API_VERSION,
            app="ESPuino-Mediahub",
            secret_required=True,
            ui_password_set=bool(store.get_settings().get("password_hash")),
        )

    @api.get("/devices")
    def devices():
        """The devices the hub knows and the card last placed on each. A device
        enters this list by fetching a manifest, so one that was never used is
        absent — there is no other way to register one."""
        return jsonify(
            [
                {
                    "esp_id": esp_id,
                    "alias": device.get("alias"),
                    "last_seen": device.get("last_seen"),
                    "last_card_id": device.get("last_card_id"),
                }
                for esp_id, device in sorted(store.list_devices().items())
            ]
        )

    @api.get("/cards")
    def cards():
        """The cards the hub holds, optionally narrowed to one device and one
        status. Both filters are read off the stored cards and nothing else:
        this API does not model card lifecycles or pin a vocabulary of states,
        it reports what the hub has.

        A device that holds no cards answers an empty list and not an error; a
        mistyped device reads the same way, which is why a client takes its
        device list from `/api/devices` rather than from its own memory."""
        device = request.args.get("device", "").strip()
        status = request.args.get("status", "").strip()
        cards = [
            card
            for card in store.list_cards().values()
            if (not device or card.get("esp_id") == device)
            and (not status or card.get("status") == status)
        ]
        cards.sort(key=lambda card: (card.get("esp_id") or "", card.get("card_id") or ""))
        return jsonify([_card_json(card) for card in cards])

    # -- the write --------------------------------------------------------
    @api.post("/cards/<esp_id>/<card_id>")
    def assign_card(esp_id, card_id):
        """Write one assignment, for one device.

        `card_id` has to be a card's own id — twelve digits — because that is
        what the store builds its key from. A card the hub has never seen is
        created by this very write, which is the ordinary case for a card a
        device has just reported.
        """
        if not manifest_lib.is_valid_card_id(card_id):
            return _error(400, "invalid_card_id", "a card id is twelve digits")
        if esp_id not in store.list_devices():
            return _error(404, "unknown_device", f"this hub has never seen a device {esp_id!r}")
        if request.form.getlist("target_esp_ids"):
            # The form writes one save to every ticked device; this API writes
            # one, so a client meaning three devices posts three times.
            # Ignoring the field would let it believe it had written three.
            return _error(
                400,
                "unsupported_field",
                "this API writes one device per call — post the card once per device",
            )

        payload, error, warning = read_assignment(request.form)
        if payload is None:
            # The hub's own sentence, the same one the form shows, so an
            # operator reading a client's log recognizes it. The validation
            # answers a payload or an error and never one without the other;
            # the fallback is for the case where that itself is the bug.
            return _error(400, "unusable_input", error or "the assignment could not be read")

        existing = store.get_card(esp_id, card_id)
        # Only a card that already carried content counts as replaced. A card
        # that is merely registered — tapped on a device, no content yet — is
        # what this write usually finds, and calling that "replaced" would cry
        # wolf for the one case the flag exists for: an assignment a human or
        # another tool had made, being written over.
        replaced = bool(existing) and existing.get("status") == "assigned"
        card = store.save_assignment(esp_id, card_id, *payload)
        kept = card.get("files") or []
        kept_paths = {entry.get("path") for entry in kept}

        return (
            jsonify(
                status=card.get("status"),
                card=_card_json(card),
                files=kept,
                # The paths the card does not hold, so "assigned, minus one
                # unusable file" is told apart from a write that landed whole.
                dropped=[path for path in _selected_paths(request.form) if path not in kept_paths],
                warning=warning,
                replaced=replaced,
            ),
            200 if replaced else 201,
        )

    return api


def _configured_secret():
    """The secret this hub answers to, or an empty string when it serves no API."""
    return os.environ.get(SECRET_ENV, "").strip()


def _selected_paths(form):
    """The paths a form named, deduplicated and in order, as far as they can be
    read — the same lenient reading `_read_assignment` does."""
    try:
        paths = json.loads(form.get("selected_paths_json", "[]"))
    except ValueError:
        return []
    if not isinstance(paths, list):
        return []
    return [path for path in dict.fromkeys(paths) if isinstance(path, str)]


def _card_json(card):
    """A card as this API shows it: the hub's record, plus its identity.

    `force_epoch` is the admin UI's own bookkeeping — it is how a card is
    pushed to a device again — and stays out of a client's way, as does the IP
    address of a device in `/api/devices`.
    """
    return {
        "esp_id": card.get("esp_id"),
        "card_id": card.get("card_id"),
        "status": card.get("status"),
        "name": card.get("name") or "",
        "kind": card.get("kind") or "files",
        "play_mode": card.get("play_mode"),
        "stream_url": card.get("stream_url") or "",
        "play_root": card.get("play_root") or "",
        "files": card.get("files") or [],
        "first_seen": card.get("first_seen"),
        "last_seen": card.get("last_seen"),
        "updated_at": card.get("updated_at"),
    }


def _error(status, word, message):
    """The one error shape: a word a program branches on, and a sentence a human
    acts on. Both are always present, so a client never parses prose."""
    return jsonify(error=word, message=message), status
