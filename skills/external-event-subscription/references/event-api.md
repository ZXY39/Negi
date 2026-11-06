# External event API reference

## Endpoint

```http
POST /api/v1/event-subscriptions/{subscription_id}/events
Authorization: Bearer <subscription token>
Idempotency-Key: <stable event key>
Content-Type: application/json
```

The route lives on the same local web server as the rest of `/api/v1` (default
`http://127.0.0.1:18790`). The credential file contains `endpoint_path`; combine it with the
operator-configured base URL.

## Request

```json
{
  "type": "page.changed",
  "occurred_at": "2026-11-06T15:00:00+08:00",
  "text": "target value is now 12",
  "data": {"url": "http://example.test/item/1", "value": 12}
}
```

- `type`: optional short event type.
- `occurred_at`: optional ISO timestamp from the external source.
- `text`: optional model-facing text.
- `data`: optional JSON object.
- At least one of `text` or `data` is required.
- Serialized `text + data` is capped at 256 KiB.
- `Idempotency-Key` is required, trimmed, 1–128 characters.

Target, instruction, trust and subscription state are fixed server-side. The request cannot
override them.

## Responses

- `202`: persisted; body `{ok, event_id, status:"pending", duplicate:false}`.
- `200`: duplicate key; body `{ok, event_id, status:<current>, duplicate:true}`.
- `400`: invalid key/type/time/shape/content.
- `401`: missing or wrong subscription token.
- `403`: external API disabled or subscription disabled.
- `404`: subscription missing or removed.
- `413`: body too large.
- `423`: project locked.
- `503`: event service unavailable; retry with backoff.

## Delivery states

| Status | Meaning |
|---|---|
| `pending` | Durable; not yet placed in the target context. `delivery_detail` carries the latest stage/error. |
| `injected` | Event bundle entered a session heartbeat context. Model failure after this point does not re-deliver. |
| `delivered` | Node mailbox accepted the event. Wake may still be suppressed by pause/acceptance rules. |
| `undeliverable` | Permanent target/subscription failure; inspect `delivery_detail.reason`. |

HTTP acceptance is not processing success. The external program should not wait for a model reply;
this lane only wakes the target.

## Idempotency

The durable unique key is `(subscription_id, Idempotency-Key)`. Reuse the same key for retries.
Different subscriptions may use the same key. Never derive the key from wall-clock time or a
retry counter.
