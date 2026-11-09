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

The credential belongs to the location; it is not a Tryton user's application key. Service
requests run as the system user, so the person who generated the token is neither logged in
nor recorded as the creator of detection events. Only surveillance administrators can show,
generate or revoke the credential. Set the location's **Service Language** when notifications
from its service should use a language other than the configured default.

The location form does not display the token by default. An administrator can use **Show
Service Token** to open a copyable read-only dialog. The dialog decrypts the Fernet ciphertext;
the ciphertext is not exposed through RPC.
The token is not stored in the wizard session.


Every request carries the key:

```text
Authorization: Bearer LOCATION_SERVICE_TOKEN
```

* The key identifies exactly one active location. All routes enforce this boundary: camera
  configuration and passwords, events, heartbeats, latest pictures, live frames and pending
  movement commands. A request naming a camera from another location returns 404.
  Mixed-location heartbeat/poll requests are rejected before any camera or mailbox is updated.
* Service tokens are encrypted at rest with `cryptography.fernet`, through `secret.py`, using
  `[videovigilance] secret_key` in `trytond.conf` (the same key as camera passwords). Keep the
  key in protected backups separately from the database; replacing it prevents recovery of
  existing credentials.
* The location stores only the Fernet-encrypted token, formatted `<location id>.<secret>`; the
  id selects the location and the secret is compared after decryption. The field is
  protected from normal RPC reads and writes. Other `res.user.application` keys are
  unchanged.
* `TRYTON_API_TOKEN` remains the original token, not its ciphertext. Use HTTPS
  when the service does not reach Tryton through a private link.
* Missing, invalid, revoked or inactive-location credentials receive 401; Tryton session
  cookies do not authenticate service requests.

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

## Upgrading

Update the `videovigilance` module (`trytond-admin -u videovigilance`, using your instance's
configuration/database), then restart Tryton. This cutover does not migrate previous
`res.user.application` service keys. After the update, generate a token for each location,
replace `TRYTON_API_TOKEN` in its Raspberry Pi `.env`, and restart that service. Its old
credential will no longer authenticate.

The detection service does not log in through JSON-RPC or use a service user. Its API requests
use the location Bearer token and execute as the system user. Remove any obsolete
detection-service Tryton user only after confirming it is not used elsewhere.
