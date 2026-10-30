# API of the detection service

The detection service (the `service/` folder, which runs on a Raspberry Pi) talks to Tryton
through plain HTTP routes under `/<database_name>/videovigilance/`, defined in `routes.py`. It
does not log in to Tryton and does not use its JSON-RPC, so it keeps working whatever
authentication people use (password, WebAuthn...).

## Access

Each **location** represents one Raspberry Pi running a detection service for the cameras
assigned to that location.

1. Open **Video Surveillance > Locations** with the **Video Surveillance: Administration**
   group, create or select the location, assign its cameras and save.
2. Click **Generate Service Token**, then **Show Service Token**. Copy the token from the
   dialog into `TRYTON_API_TOKEN` in that Raspberry's `.env`. Keep `TRYTON_API_URL` pointing at
   this database's API
   (`http://127.0.0.1:8010/cameras/videovigilance` by default).
3. Start or restart the service on that Raspberry. Its configuration contains only the active
   cameras assigned to this location.

Generating another token immediately invalidates the previous token of that location:
update the Raspberry's `.env` and restart its service. **Revoke Service Token** blocks the
credential immediately; generate a new one to reconnect.
Neither operation changes other locations' credentials. Deactivating a location also blocks
its credential while it remains inactive.

The token uses Tryton's existing `res.user.application` model with application
**Video Surveillance: Detection Service**, linked to the location. The user who first
generates it owns the application and appears as the creator of events; the service does
not log in as that user. Only surveillance administration users can read the token, use
the generation/revocation buttons, or assign an application to a location. Applications
of other types cannot be assigned to a location.

The location form does not display the token by default. An administrator can use **Show
Service Token** to open a copyable read-only dialog. The dialog returns the original
credential, decrypted from Fernet storage, not the ciphertext or its fingerprint; it is
not stored in the wizard session. Close the dialog after copying it.

Every request carries the key:

```text
Authorization: Bearer LOCATION_SERVICE_TOKEN
```

* The key identifies the location, not just a user. Two keys owned by the same user remain
  isolated. A key without an associated location is refused (401), with no global fallback.
* All routes enforce this boundary: camera configuration and passwords, events, heartbeats,
  latest pictures, live frames and pending movement commands. A request naming a camera
  from another location returns 404. Mixed-location heartbeat/poll requests are rejected
  before any camera or mailbox is updated.
* The groups and access rules of the application's user do not take part in service requests.
* Cancelling the application also revokes its key at once.
* Service tokens are encrypted at rest with `cryptography.fernet`, through `secret.py`, using
  `[videovigilance] secret_key` in `trytond.conf` (the same key as camera passwords). Keep the
  key in protected backups separately from the database; replacing it prevents recovery of
  existing credentials.
* `res.user.application.key` stores a SHA-256 fingerprint for service-key lookup, not the
  usable token. `videovigilance_key_encrypted` stores its Fernet ciphertext and is not readable
  or writable through normal RPC access. The location form decrypts it for authorized
  administrators. Other applications retain their existing key handling.
* `TRYTON_API_TOKEN` remains the original token, not its fingerprint or ciphertext. Use HTTPS
  when the service does not reach Tryton through a private link.
* Nothing is served for a key of another application, a cancelled key or no key (401), nor to a
  Tryton session cookie.

## Routes

| Route | What it does |
|---|---|
| `GET config` | This location's active cameras with their credentials (decrypted). `ETag` / `If-None-Match`: 304 when nothing changed. `Cache-Control: no-store` |
| `PUT events/<uid>` | Stores a detection. Multipart: part `event` (JSON: `camera` id, `type`, `started_at`, `ended_at`, `max_persons`, `max_confidence`; ISO 8601 times) and part `snapshot` (JPEG). 201 when stored, 200 when that uid already belongs to this location, so sending it again is harmless. Only person events |
| `POST heartbeat` | `{"cameras": [{"code", "online", "detail", "device"}]}`. Records that the service is alive and the state of each camera. Answers `camera_interval`, `camera_failures` and `service_interval` |
| `PUT cameras/<id>/frame` | The latest picture of a camera (`image/jpeg`, up to 1 MB). 204 |
| `POST live/poll` | `{"cameras": {"<id>": {"error", "message"}}}`. Answers, per camera, whether someone is watching and the movement commands waiting |
| `POST live/<id>/frame` | A live frame (`image/jpeg`). Answers `{"wanted": bool}` |

Errors are JSON, `{"error": why}`:

* 400: what was sent is not valid (a field, a type, a date, a JPEG...).
* 401: no valid key, no associated location, or an inactive location.
* 404: no such camera in the authenticated location, or an event UID belonging to another location.
* 409: another request is storing that same event; send it again.
* 413: the request is too big.

The live routes lock the referenced cameras until the mailbox operation completes, so
reassigning a camera cannot race an authenticated service request.

The service's global health status/watchdog and heartbeat intervals remain shared in
Configuration; this change isolates credentials and camera access, not service-health
monitoring per Raspberry.

## Upgrading existing location credentials to encrypted storage

Ensure `[videovigilance] secret_key` is configured before updating the module. The module
update encrypts existing detection-service keys and replaces their plaintext database
values with fingerprints in the same transaction. Existing location credentials remain
valid: no token change or Raspberry reconfiguration is needed. The migration is idempotent;
it does not encrypt already migrated values again. Protect or retire older database backups,
which may still contain plaintext tokens.


## Upgrading from a shared service key

Update the `videovigilance` module in the database (`trytond-admin -u videovigilance`, using
your instance's configuration/database), then restart Tryton. No Tryton core change or new
service environment variable is required.

Generate a credential from each location and replace `TRYTON_API_TOKEN` on its Raspberry,
then restart that service. Existing application keys without a location are no longer
accepted: they return 401 until replaced by a location credential. There is no automatic
assignment of an old shared key to a location.


## Upgrading from the JSON-RPC

The service used to log in over JSON-RPC and to use the relay routes under `/avisos/relay`. Both
are gone, with the `Videovigilància: servei` group. Update the module, create the key, clear the
password of the service user (it no longer logs in) and then deploy the service with the new
`.env` (`TRYTON_API_TOKEN`); until then the service cannot talk to Tryton.
