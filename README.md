# EVE Diagnostics — Booking & Payments API

Backend service for **diagnostic-test bookings with simulated payments**, built with
**FastAPI + SQLAlchemy 2 + PostgreSQL (Neon)**, with Redis caching, rate limiting, Celery
background jobs, JWT auth, an **idempotent payment webhook**, structured logging, Docker and tests.

- Interactive docs: **`/docs`** (Swagger UI) · **`/redoc`** · `/openapi.json`
- Tests: 55 (53 run on SQLite; +2 concurrency tests on PostgreSQL) — all green on both

---

## 1. Run it locally

### Option A — Python + Neon (fastest)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env        # then edit DATABASE_URL (Neon), JWT_SECRET, WEBHOOK_SECRET
python -m scripts.seed      # optional: sample tests, centres and prices
uvicorn app.main:app --reload
```

Open <http://localhost:8000/docs>.

**Neon setup:** create a project at neon.tech → copy the connection string (use the
*pooled* one) into `DATABASE_URL`. `postgresql://…` is auto-converted to the psycopg3 driver;
keep `?sslmode=require`. Tables are created on startup (`AUTO_CREATE_TABLES=true`).

`REDIS_URL` is optional: without it, caching and rate limiting fall back to in-process memory
(fine for one process; use Redis for multiple workers).

### Option B — Docker Compose (API + worker + beat + Redis)

```bash
cp .env.example .env
# Neon:            docker compose up --build api worker beat redis --no-deps
# Local Postgres:  (remove DATABASE_URL from .env)  docker compose up --build
```

### Become an admin
Admin-only routes create tests/centres and set prices. Any email listed in `ADMIN_EMAILS`
becomes `ADMIN` when it signs up (e.g. `ADMIN_EMAILS=admin@example.com`). Or just run
`python -m scripts.seed`.

### Run tests
```bash
pytest                                                        # SQLite in-memory, ~3s
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost/eve_test pytest   # + concurrency tests
```
CI (`.github/workflows/ci.yml`) runs both.

### Environment variables
| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local Postgres | Neon/Postgres URL |
| `JWT_SECRET` / `ACCESS_TOKEN_EXPIRE_MINUTES` | dev value / 60 | JWT signing (required in `production`) |
| `WEBHOOK_SECRET` | dev value | HMAC key for webhook signatures |
| `ADMIN_EMAILS` | – | comma-separated auto-admins |
| `REDIS_URL` / `CELERY_BROKER_URL` | none | cache + rate limit / Celery broker |
| `CACHE_TTL_SECONDS` | 60 | catalogue cache TTL |
| `RATE_LIMIT_ENABLED`, `AUTH_RATE_LIMIT_PER_MINUTE`, `PAYMENT_RATE_LIMIT_PER_MINUTE` | true, 10, 20 | rate limits |
| `PAYMENT_SUCCESS_RATE` | 0.8 | chance a random simulated payment succeeds |
| `ALLOW_SIMULATED_OUTCOME` | true | honour `simulate_outcome` (set false in prod) |
| `WEBHOOK_MAX_ATTEMPTS` | 5 | retry cap for failed webhook events |
| `PENDING_BOOKING_TTL_MINUTES` | 30 | auto-cancel unpaid bookings after this |

---

## 2. Architecture

```
            ┌───────────────┐   JWT    ┌────────────────────────────┐
 client ───▶│  FastAPI      │─────────▶│ routes  (thin: validate,   │
            │  middleware:  │          │          auth, serialise)  │
            │  request-id,  │          └─────────────┬──────────────┘
            │  JSON logs    │                        ▼
            └──────┬────────┘          ┌────────────────────────────┐
                   │                   │ services (business rules,  │
   payment provider│ HMAC-signed       │  state machines, locking)  │
   ───webhook──────┘                   └───────┬─────────────┬──────┘
                                               ▼             ▼
                                    PostgreSQL (Neon)      Redis
                                    SQLAlchemy models      cache · rate-limit · broker
                                               ▲
                              Celery beat/worker ── retry failed webhooks, expire stale bookings
```

```
app/
  main.py            app factory, middleware, /health
  models.py          ORM models + constraints
  schemas.py         Pydantic request/response models
  core/              config, security (JWT/bcrypt), cache, rate_limit, logging, errors
  db/session.py      engine (Neon-friendly pool), session dependency
  api/deps.py        auth + pagination dependencies
  api/routes/        auth, catalog (tests+centres), bookings, payments (+webhook)
  services/          auth, bookings, payments, webhooks  ← all business logic
  tasks/             Celery app + jobs
scripts/             seed, send_webhook (signed), export_schema
tests/               unit + API tests (+ Postgres concurrency tests)
docs/schema.sql      generated PostgreSQL DDL
```

Routes stay thin; rules live in `services/` so they can be tested without HTTP and reused by Celery.

---

## 3. API reference

All errors look like `{"detail": "...", "code": "machine_code"}` (validation errors: FastAPI's
standard 422 body). Lists are paginated: `?limit=20&offset=0` → `{items, total, limit, offset}`.

| Method & path | Auth | Description |
|---|---|---|
| `POST /auth/signup` | – | Create account (email, password ≥8 w/ letter+digit, full_name) |
| `POST /auth/login` | – | JSON login → JWT |
| `POST /auth/token` | – | OAuth2 form login (Swagger *Authorize*) |
| `GET /auth/me` | user | Current user |
| `GET /tests` · `GET /tests/{id}` | – | List (`?q=`) / get diagnostic tests |
| `POST /tests` | admin | Create a test |
| `GET /centres` | – | List; filters `q`, `city`, `test_id`; includes tests + prices |
| `GET /centres/{id}` | – | One centre with its tests |
| `POST /centres` | admin | Create a centre |
| `PUT /centres/{id}/tests/{test_id}` | admin | Offer a test at a centre / change price (idempotent upsert) |
| `DELETE /centres/{id}/tests/{test_id}` | admin | Stop offering (soft delete) |
| `POST /bookings` | user | Book a test (status `PENDING`) |
| `GET /bookings` · `GET /bookings/{id}` | user | Own bookings (`?status=`) / one booking |
| `POST /bookings/{id}/cancel` | user | Cancel |
| `POST /payments/` | user | **Simulated payment** → SUCCESS / FAILED, updates booking |
| `GET /payments/` · `GET /payments/{id}` | user | Own payments (`?booking_id=`) |
| `POST /payments/webhook/` | HMAC | **Idempotent** provider webhook |
| `GET /health` | – | DB + cache status |

### Example walkthrough

```bash
B=http://localhost:8000

# 1. signup + login
curl -s -X POST $B/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"Passw0rd123","full_name":"Alice"}'
TOKEN=$(curl -s -X POST $B/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","password":"Passw0rd123"}' | jq -r .access_token)

# 2. browse
curl -s "$B/centres?city=Bengaluru&limit=5"

# 3. book (appointment_at needs a timezone offset, 30 min – 180 days ahead)
curl -s -X POST $B/bookings -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"centre_id":1,"test_id":1,"appointment_at":"2026-10-15T10:30:00+05:30"}'
# → {"id":1,"amount":"350.00","status":"PENDING",...}

# 4. pay (Idempotency-Key makes client retries safe)
curl -s -X POST $B/payments/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-42' -d '{"booking_id":1}'
# → {"status":"SUCCESS", "provider_ref":"pay_...", ...}   booking → CONFIRMED  (or FAILED → retry)
```

`simulate_outcome` (`SUCCESS` | `FAILED` | `PENDING`) forces a result for demos/tests. `PENDING`
leaves the payment open until the webhook settles it:

```bash
curl -s -X POST $B/payments/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"booking_id":1,"simulate_outcome":"PENDING"}'          # note provider_ref

python -m scripts.send_webhook pay_xxxxxxxx SUCCESS --event-id evt_1   # processed
python -m scripts.send_webhook pay_xxxxxxxx SUCCESS --event-id evt_1   # duplicate (no change)
```

Raw webhook (signature = hex HMAC-SHA256 of the **exact body** with `WEBHOOK_SECRET`):

```bash
BODY='{"event_id":"evt_1","payment_ref":"pay_xxxxxxxx","status":"SUCCESS"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $2}')
curl -s -X POST $B/payments/webhook/ -H 'Content-Type: application/json' \
  -H "X-Webhook-Signature: $SIG" -d "$BODY"
# → {"result":"processed"|"duplicate"|"ignored","event_id":"evt_1","payment_status":"SUCCESS","booking_status":"CONFIRMED",...}
```

---

## 4. Database design

Full DDL: [`docs/schema.sql`](docs/schema.sql).

```mermaid
erDiagram
  users ||--o{ bookings : makes
  users ||--o{ payments : makes
  centres ||--o{ centre_tests : offers
  diagnostic_tests ||--o{ centre_tests : "offered as"
  centre_tests ||--o{ bookings : "(centre_id,test_id)"
  bookings ||--o{ payments : "paid by"
  payments ||--o{ webhook_events : "payment_ref → provider_ref"

  users { int id PK  string email UK  string password_hash  string role }
  diagnostic_tests { int id PK  string code UK  string name UK }
  centres { int id PK  string name  string city  string address  UK "(name,city)" }
  centre_tests { int id PK  int centre_id FK  int test_id FK  numeric price  bool is_active  UK "(centre_id,test_id)" }
  bookings { int id PK  int user_id FK  int centre_id  int test_id  numeric amount  timestamptz appointment_at  string status }
  payments { int id PK  int booking_id FK  int user_id FK  numeric amount  string status  string provider_ref UK  string idempotency_key }
  webhook_events { int id PK  string event_id UK  string payment_ref  jsonb payload  string status  int attempts }
```

Key decisions
- **Price lives on `centre_tests`**, so one test can cost differently per centre. `is_active` soft-deletes so history stays valid.
- **`bookings.amount` is a price snapshot**; later price changes don't alter existing bookings.
- **Composite FK** `bookings(centre_id,test_id) → centre_tests(centre_id,test_id)`: a booking can't reference a test the centre doesn't offer (also checked in the service for a friendly 404).
- **Money is `NUMERIC(10,2)`** (never float), with `CHECK (price > 0)`.
- **Enums as `VARCHAR + CHECK`** rather than native PG enums (no `ALTER TYPE` pain).
- **Partial unique indexes** enforce invariants in the DB, not just in code:
  - `uq_payment_open_per_booking` — at most one `PENDING`/`SUCCESS` payment per booking ⇒ no double charge even under concurrent requests (FAILED attempts don't count, so retries work).
  - `uq_booking_active_slot` — a user can't hold two live bookings for the same test/centre/slot.
- **`UNIQUE(user_id, idempotency_key)`** on payments for client-side idempotency; **`UNIQUE(event_id)`** on `webhook_events` for webhook idempotency.
- Indexes for the real access paths: `bookings(user_id, created_at)`, `bookings(status, created_at)` (expiry job), `centres(city)`, `centre_tests(test_id)`.

### State machines
```
Booking : PENDING ──pay ok──▶ CONFIRMED          PENDING ──pay fail──▶ FAILED ──pay ok──▶ CONFIRMED
          PENDING/FAILED/CONFIRMED ──cancel──▶ CANCELLED          (CANCELLED is terminal)
Payment : PENDING ──▶ SUCCESS | FAILED                             (SUCCESS/FAILED are terminal)
```
`sync_booking_with_payment()` is the single place that maps payment outcomes to booking status
(never downgrades `CONFIRMED`; a success for a `CANCELLED` booking keeps it cancelled and logs `refund_required`).

---

## 5. Idempotency & concurrency (the important part)

**Webhook** (`services/webhooks.py`, inbox pattern):
1. Verify HMAC signature on the raw body (constant-time compare) → else `401`.
2. `INSERT` into `webhook_events` keyed by `UNIQUE(event_id)`. A redelivery hits the constraint and is answered from the stored event (`result: "duplicate"`) — nothing is re-applied.
3. Lock the event row (`SELECT … FOR UPDATE`), re-check its status, then apply the change to payment + booking **in one transaction**. Concurrent deliveries of the same event serialise; exactly one applies it.
4. Applying is also state-idempotent: only a `PENDING` payment is changed. A *different* event for an already-settled payment is recorded as `IGNORED` (conflicts are logged), so late/out-of-order events can't corrupt state.
5. Failure (e.g. unknown `payment_ref`) → event stored as `FAILED`, `attempts++`, `404/500` returned so the provider retries with the same `event_id` (re-processed). Celery beat also retries `FAILED` events with exponential back-off (`10s·2^attempts`, capped by `WEBHOOK_MAX_ATTEMPTS`).

**Payments:** booking row is locked (`FOR UPDATE`) while paying; the partial unique index is the backstop.
`Idempotency-Key` replays the original response (`200`) instead of creating a second payment.

The concurrency tests (`tests/test_concurrency.py`) fire 8 parallel duplicate webhooks and 8 parallel payments at PostgreSQL and assert exactly one is applied. (Writing them found a real stale-read bug — locked rows already in the session's identity map weren't refreshed — fixed with `populate_existing`.)

---

## 6. Edge cases handled

| Case | Behaviour |
|---|---|
| Invalid body / types / weak password / bad email | `422` with field errors |
| Naive, past (<30 min) or >180-day appointment | `422` |
| Unknown booking/payment/centre/test id | `404` (`code` says which) |
| Non-integer id in path | `422` |
| Booking someone else's / other user's booking or payment | `404` (not 403, so ids can't be enumerated) |
| Missing / invalid / expired / forged JWT | `401` + `WWW-Authenticate` |
| Non-admin on admin route | `403` |
| Duplicate signup / centre / test / live booking slot | `409` |
| Pay a confirmed / cancelled booking, or one with a pending payment | `409` |
| Failed payment | booking → `FAILED`; user can pay again |
| Client picks amount | ignored — amount comes from the booking |
| Repeated / concurrent webhook | one application, others `duplicate` |
| Webhook with bad/missing signature | `401`, nothing stored |
| Webhook conflicting with settled payment | `ignored`, logged |
| Webhook before payment exists | `404`, stored `FAILED`, retried |
| Login for unknown vs wrong password | identical response + dummy hash (no user enumeration/timing leak) |
| Brute force | per-IP rate limit on auth (`429` + `Retry-After`), per-user on payments |
| Redis down | cache misses / rate limiter fails open — API stays up |
| Unpaid bookings | Celery job cancels `PENDING` bookings older than TTL (skips ones with a payment in flight) |

---

## 7. Bonus items implemented
Redis caching (versioned-namespace invalidation) · Celery jobs (webhook retry, booking expiry) · Docker & docker-compose ·
Swagger/OpenAPI · unit/integration/concurrency tests + CI · structured JSON logging with request ids ·
pagination · rate limiting · webhook retry handling with back-off.

---

## 8. Assumptions
- One test per booking; a booking is for the authenticated user (no separate "patient" entity — `user` is the patient).
- Slot availability/capacity per centre isn't modelled; only "same user, same slot" duplicates are blocked.
- Payment amount == booking amount, single currency (INR implied).
- A `FAILED` booking is retryable; `CANCELLED` is terminal. Cancelling a confirmed booking is allowed; refunds are out of scope (logged as `refund_required`).
- The "provider" signs webhooks with a shared HMAC secret and sends `event_id`, `payment_ref`, `status`.
- Access tokens only (no refresh tokens); admins are bootstrapped via `ADMIN_EMAILS`.
- `simulate_outcome` is a testing aid; disable with `ALLOW_SIMULATED_OUTCOME=false`.
- `X-Forwarded-For` isn't trusted for rate limiting; behind a proxy configure uvicorn `--proxy-headers`.

## 9. What I'd improve with more time
- **Alembic migrations** instead of `create_all` on startup.
- Refresh tokens / token revocation, email verification, password reset.
- Slot capacity & centre working hours; reschedule endpoint; refunds for cancelled-after-paid bookings.
- Outbox + async webhook processing (respond `202` fast, process in Celery) and a dead-letter view for exhausted events.
- Timestamp/replay-window check in webhook signatures; secret rotation.
- Sliding-window/token-bucket rate limiter; trust-proxy handling.
- Metrics (Prometheus) and tracing (OpenTelemetry); Sentry.
- Keyset pagination for large lists; admin endpoints for editing/deleting centres and tests.
