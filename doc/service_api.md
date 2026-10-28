# API of the detection service

The detection service (the `service/` folder, which runs on a Raspberry Pi) talks to Tryton
through plain HTTP routes under `/<database_name>/videovigilance/`, defined in `routes.py`. It
does not log in to Tryton and does not use its JSON-RPC, so it keeps working whatever
authentication people use (password, WebAuthn...).

## Access

Create a **user application** for the user that stands for the service (Administration > Users >
Applications): application **Videovigilància: servei de detecció**, state *Validated*. Tryton
generates the key. Give it to the service as `TRYTON_API_TOKEN` (see `service/config.py`;
`TRYTON_API_URL` defaults to `http://127.0.0.1:8010/cameras/videovigilance`).

Every request carries the key:

```text
Authorization: Bearer APPLICATION_KEY
```

* The key says who the service is; what it can do is what the routes do. The groups and the
  access rules of its user do not take part, so the user needs no group. The user only appears
  as the creator of the events.
* Cancelling the application revokes the key at once.
* Keys are stored in clear text, like those of Tryton's other applications. Use HTTPS when the
  service does not reach Tryton through a private link.
* Nothing is served for a key of another application, a cancelled key or no key (401), nor to a
  Tryton session cookie.

## Routes

| Route | What it does |
|---|---|
| `GET config` | The active cameras with their credentials (decrypted). `ETag` / `If-None-Match`: 304 when nothing changed. `Cache-Control: no-store` |
| `PUT events/<uid>` | Stores a detection. Multipart: part `event` (JSON: `camera` id, `type`, `started_at`, `ended_at`, `max_persons`, `max_confidence`; ISO 8601 times) and part `snapshot` (JPEG). 201 when stored, 200 when that uid was already stored, so sending it again is harmless. Only person events |
| `POST heartbeat` | `{"cameras": [{"code", "online", "detail", "device"}]}`. Records that the service is alive and the state of each camera. Answers `camera_interval`, `camera_failures` and `service_interval` |
| `PUT cameras/<id>/frame` | The latest picture of a camera (`image/jpeg`, up to 1 MB). 204 |
| `POST live/poll` | `{"cameras": {"<id>": {"error", "message"}}}`. Answers, per camera, whether someone is watching and the movement commands waiting |
| `POST live/<id>/frame` | A live frame (`image/jpeg`). Answers `{"wanted": bool}` |

Errors are JSON, `{"error": why}`:

* 400: what was sent is not valid (a field, a type, a date, a JPEG...).
* 401: no valid key.
* 404: no such camera.
* 409: another request is storing that same event; send it again.
* 413: the request is too big.

The live routes use a read-only transaction: they only touch the files of the mailbox (`live.py`).

## Upgrading from the JSON-RPC

The service used to log in over JSON-RPC and to use the relay routes under `/avisos/relay`. Both
are gone, with the `Videovigilància: servei` group. Update the module, create the key, clear the
password of the service user (it no longer logs in) and then deploy the service with the new
`.env` (`TRYTON_API_TOKEN`); until then the service cannot talk to Tryton.
