# Transaction Webhook Service

This service takes transaction webhooks from a payment processor, answers immediately, and finishes the work in the background.

I built it for a backend assessment. The hard part is not the HTTP endpoint. The hard part is acknowledging the webhook quickly without losing the transaction if the app, Redis, or a worker dies halfway through.

The webhook handler only writes to PostgreSQL. A separate process later picks up that row and hands it to a Celery worker. The worker waits 30 seconds, standing in for a slow external payment call, and then marks the transaction processed.

The API is deployed on Render:

https://transaction-webhook-service-2.onrender.com

Source code: https://github.com/jui1/transaction-webhook-service

## How it works

Three processes share one database:

| Process | What it does | Command |
| --- | --- | --- |
| API | Checks the webhook, saves it, and returns | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| Dispatcher | Looks for saved work and puts it on the queue | `python -m app.workers.dispatcher` |
| Worker | Runs the 30-second processing step | `celery -A app.workers.celery_app:celery_app worker --loglevel=info -Q transactions` |

The API does not call the worker directly. It writes two rows in the same database transaction:

1. the payment, in `transactions`
2. a "please process this" note, in `outbox_events`

If the API crashes before that commit, nothing is saved and the caller does not get a success response. If it crashes after the commit, the note is still in PostgreSQL. The dispatcher finds it on the next poll, even if Redis never received the job.

```mermaid
sequenceDiagram
    participant Client
    participant API
    participant Postgres
    participant Dispatcher
    participant Redis
    participant Worker
    Client->>API: POST /v1/webhooks/transactions
    API->>Postgres: Save transaction and outbox row
    API-->>Client: 202 Accepted
    Dispatcher->>Postgres: Pick up pending outbox rows
    Dispatcher->>Redis: Enqueue Celery job
    Worker->>Postgres: Take a processing lease
    Worker->>Worker: Sleep for 30 seconds
    Worker->>Postgres: Mark PROCESSED
```

## Technology choices

- **FastAPI** handles the three HTTP endpoints and checks the JSON with Pydantic.
- **PostgreSQL** stores the transaction and the outbox row. It is the record of what was accepted. Redis is not.
- **SQLAlchemy 2** is the database layer. **Alembic** creates the tables.
- **Redis and Celery** run the slow step outside the request. Redis can lose a message, so the app does not treat the queue as the only copy of the work.
- **A dispatcher process** reads the outbox table and publishes jobs. It can be restarted on its own.
- **Pytest** checks the API and the failure cases. Most tests use SQLite and do not wait 30 seconds.

## Request flow

1. The client sends `POST /v1/webhooks/transactions`.
2. Pydantic rejects a bad body with HTTP 422.
3. The API inserts the transaction with status `PROCESSING`. In the same commit it inserts one outbox row with status `PENDING`.
4. The API returns HTTP 202. At this point Redis has not been contacted.
5. About once a second, the dispatcher claims due outbox rows. On PostgreSQL it uses `FOR UPDATE SKIP LOCKED`, so two dispatcher processes do not grab the same row.
6. It marks those rows `DISPATCHED`, commits, then sends a Celery job.
7. If Redis refuses the job, the row goes back to `PENDING` and will be tried again.
8. The worker loads the transaction. If it is already `PROCESSED`, the worker stops.
9. Otherwise it sets a lease (`processing_token` and `lease_expires_at`) with a conditional update. A second worker sees the active lease and leaves the row alone.
10. The worker sleeps for `PROCESSING_DELAY_SECONDS`. The default is 30. This sleep is only in the worker.
11. If the worker still holds the lease, it sets `status` to `PROCESSED`, fills `processed_at`, and marks the outbox row `COMPLETED`. Those updates commit together.
12. `GET /v1/transactions/{transaction_id}` reads whatever is stored. `processed_at` stays `null` until step 11 finishes.

`PROCESSING` means "saved, but not finished". That includes both queued work and work a worker is currently running. There is no third public status.

## API

Local base URL: `http://127.0.0.1:8000`

OpenAPI docs are at `http://127.0.0.1:8000/docs` while the API is running.

### Health check

`GET /`

```bash
curl -sS http://127.0.0.1:8000/
```

HTTP 200:

```json
{
  "status": "HEALTHY",
  "current_time": "2024-01-15T10:30:00Z"
}
```

`current_time` is the current UTC time, ending in `Z`. This check does not query the database. It only tells you the API process is up.

### Receive a webhook

`POST /v1/webhooks/transactions`

```bash
curl -sS -D - \
  -H 'Content-Type: application/json' \
  -d '{
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500,
    "currency": "INR"
  }' \
  http://127.0.0.1:8000/v1/webhooks/transactions
```

A new transaction returns HTTP 202:

```json
{
  "status": "accepted",
  "transaction_id": "txn_abc123def456",
  "message": "Transaction accepted for processing"
}
```

Send the same body again. The status code is still 202, and the body says the id was already received:

```json
{
  "status": "duplicate",
  "transaction_id": "txn_abc123def456",
  "message": "Transaction already received"
}
```

The second call does not create another row or another job.

Validation rules:

- `transaction_id`, `source_account`, and `destination_account` are 1 to 128 characters. Allowed characters are letters, numbers, `.`, `_`, and `-`.
- `amount` must be greater than 0 and can have at most two decimal places.
- `currency` must be three uppercase letters, such as `INR` or `USD`.

A body that breaks those rules returns HTTP 422. A database outage returns HTTP 503:

```json
{
  "detail": "Persistent storage is temporarily unavailable"
}
```

The response should come back in well under 500ms during normal local use, because the handler only does a database commit.

### Look up a transaction

`GET /v1/transactions/{transaction_id}`

```bash
curl -sS http://127.0.0.1:8000/v1/transactions/txn_abc123def456
```

Right after the webhook, before the worker finishes:

```json
{
  "transaction_id": "txn_abc123def456",
  "source_account": "acc_user_789",
  "destination_account": "acc_merchant_456",
  "amount": 1500,
  "currency": "INR",
  "status": "PROCESSING",
  "created_at": "2024-01-15T10:30:00Z",
  "processed_at": null
}
```

About 30 seconds after the worker starts, the same URL returns `"status": "PROCESSED"` and a `processed_at` timestamp.

An unknown id returns HTTP 404:

```json
{
  "detail": "Transaction not found"
}
```

The body is one JSON object. The assessment sample shows square brackets around the object. This route does not return a list. A missing id is a 404, not an empty list.

Whole amounts are returned as numbers such as `1500`. An amount of `150.50` is stored as a decimal and comes back as `150.5`, because JSON numbers do not keep the trailing zero.

## Duplicate prevention

The same `transaction_id` must end up as one stored transaction, even if the webhook is delivered twice at the same time.

- `transactions.transaction_id` is unique. The insert is `INSERT ... ON CONFLICT DO NOTHING`. One request wins. The other sees the existing row and returns the duplicate acknowledgement.
- The outbox table also has a unique `transaction_id` and a unique link to the transaction. One webhook creates one processing note.
- The dispatcher does not enqueue a row that is already `COMPLETED`. A row that was just marked `DISPATCHED` is left alone until it looks stuck.
- The worker only takes a row that is still `PROCESSING` and whose lease is empty or expired.
- The final update is conditional: status becomes `PROCESSED` only when this worker still owns the lease. A repeat job does not change `processed_at`.

Redis task ids are not what makes this safe. The database checks above are.

If a worker is killed after the 30-second sleep returns, but before the completion commit, another worker can run the sleep again after the lease expires. The row still moves to `PROCESSED` only once.

## Background jobs and the outbox

The outbox table is the list of work that has been accepted but not finished.

| Outbox status | Meaning |
| --- | --- |
| `PENDING` | Saved, not successfully handed to Redis yet |
| `DISPATCHED` | Handed to Redis, or claimed for another try |
| `COMPLETED` | The transaction is `PROCESSED` |

The dispatcher polls every `OUTBOX_POLL_INTERVAL_SECONDS` (1 second by default) and takes up to `OUTBOX_BATCH_SIZE` rows (50).

A `DISPATCHED` row is published again only after the redelivery window. That window is at least `OUTBOX_VISIBILITY_TIMEOUT_SECONDS` (180). It is also at least 30 seconds longer than the worker lease, so a worker that is still inside the 30-second sleep is not treated as dead.

The worker lease length is the processing delay plus `PROCESSING_LEASE_SECONDS` (120). With the defaults, the lease is 150 seconds and the redelivery window is 180 seconds.

Celery is configured with `acks_late` and `reject_on_worker_lost`. If the worker process dies, the job can be delivered again. The task retries a thrown processing error up to 5 times, with a backoff of 1, 2, 4, 8, 16, then 60 seconds. After that, the Celery task stops, but the outbox row is still unfinished, so the dispatcher publishes another attempt later.

The worker queue name is `transactions`. The Celery process has to listen on that queue (`-Q transactions`). `CELERY_TASK_QUEUE` must match.

## When something goes wrong

- **Crash before commit.** The database rolls back. The client does not get HTTP 202.
- **Crash after commit, before Redis.** The outbox row is still there. The dispatcher publishes it after a restart.
- **Redis is down.** The dispatcher records the error on the outbox row, sets it back to `PENDING`, and tries later. The transaction stays `PROCESSING`.
- **Redis accepts the job and then loses it.** The row stays `DISPATCHED` until the redelivery window, then it is published again.
- **Worker dies during the sleep.** The lease expires. A later run takes the row and finishes it.
- **Worker hits a temporary error.** It clears its lease and Celery retries. It does not insert a second transaction.
- **A `PROCESSING` transaction has no outbox row.** The dispatcher creates one and publishes it. The link to the transaction is unique, so this repair does not create two notes.

## Database

Migration file: `alembic/versions/20261020_0001_initial_schema.py`

`transactions`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key |
| `transaction_id` | string | Unique id from the webhook |
| `source_account` | string | |
| `destination_account` | string | |
| `amount` | `NUMERIC(18, 2)` | Decimal, not a float |
| `currency` | string(3) | |
| `status` | string | `PROCESSING` or `PROCESSED` |
| `created_at` | timestamptz | UTC |
| `processed_at` | timestamptz or null | Set when processing finishes |
| `processing_token` | UUID or null | Which worker holds the lease |
| `lease_expires_at` | timestamptz or null | When that lease stops counting |

`outbox_events`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | UUID | Primary key |
| `transaction_pk` | UUID | Unique foreign key to `transactions.id` |
| `transaction_id` | string | Unique copy of the business id |
| `event_type` | string | `transaction.process` |
| `status` | string | `PENDING`, `DISPATCHED`, or `COMPLETED` |
| `attempts` | integer | How many times publishing was claimed |
| `last_error` | text or null | Last Redis error |
| `available_at` | timestamptz | Do not claim this row before this time |
| `created_at` | timestamptz | |
| `dispatched_at` | timestamptz or null | When it was last claimed |
| `completed_at` | timestamptz or null | When processing finished |

Indexes: unique `transaction_id` on both tables, `(status, created_at)` on transactions, and `(status, available_at)` on the outbox.

## Local setup

You need Python 3.12 or newer. Docker is needed for PostgreSQL, Redis, and the full app. The pytest suite uses SQLite and can run without Docker.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

### Everything in Docker

From the project root, with Docker running:

```bash
docker compose up --build
```

That starts PostgreSQL, Redis, the API on port 8000, the Celery worker, and the dispatcher. Each app container runs `alembic upgrade head` before its own command.

Then try the curl commands above. A new webhook should return 202 at once. The lookup should show `PROCESSING` first, then `PROCESSED` about 30 seconds after the worker picks it up. The dispatcher polls once a second, so the wait is the poll plus the 30-second sleep.

Stop the containers:

```bash
docker compose down
```

Stop them and delete the database volume:

```bash
docker compose down -v
```

Check that the compose file parses:

```bash
docker compose config
```

### API on your machine, database in Docker

```bash
docker compose up -d postgres redis
```

In three terminals, from the project root with the virtualenv active:

```bash
set -a && source .env && set +a
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

```bash
set -a && source .env && set +a
celery -A app.workers.celery_app:celery_app worker --loglevel=info -Q transactions
```

```bash
set -a && source .env && set +a
python -m app.workers.dispatcher
```

Run the migration yourself in this setup. The host processes do not run Alembic on startup. The containers do.

`.env.example` points `DATABASE_URL` at `localhost`, which matches the published Postgres port. Inside Compose, the API uses the hostname `postgres`, not `localhost`. Compose sets that itself and does not need your `.env` file.

## Environment variables

Copy `.env.example` to `.env` for the host-process setup. Do not commit `.env`. The values below are local defaults, including the database password `webhook`.

| Variable | Default | What it does |
| --- | --- | --- |
| `APP_ENV` | `local` | Label for the environment |
| `LOG_LEVEL` | `INFO` | Python log level |
| `DATABASE_URL` | `postgresql+psycopg://webhook:webhook@localhost:5432/webhook` | Database URL. `postgres://` and `postgresql://` are rewritten to the psycopg v3 driver |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker and result backend |
| `CELERY_TASK_QUEUE` | `transactions` | Queue name. The worker must use the same name |
| `PROCESSING_DELAY_SECONDS` | `30` | How long the worker sleeps. The HTTP request does not wait for this |
| `PROCESSING_LEASE_SECONDS` | `120` | Extra time added to the delay to form the worker lease |
| `OUTBOX_POLL_INTERVAL_SECONDS` | `1` | How long the dispatcher sleeps when there is nothing to publish |
| `OUTBOX_BATCH_SIZE` | `50` | How many outbox rows to claim per poll |
| `OUTBOX_VISIBILITY_TIMEOUT_SECONDS` | `180` | Minimum age of a `DISPATCHED` row before it can be published again |

On a real host, replace `DATABASE_URL` and `REDIS_URL` with the managed services. Do not keep the local password.

## Migrations

Apply the schema to the database in `DATABASE_URL`:

```bash
alembic upgrade head
```

Remove the tables created by this migration:

```bash
alembic downgrade base
```

## Tests

From the project root, with the dev requirements installed:

```bash
pytest
```

The normal suite does not sleep for 30 seconds. It uses SQLite. The processing delay is set to 0, except for one test that checks the worker is passed a delay of 30 and does not actually sleep.

`test_real_thirty_second_processing` is skipped unless you ask for it:

```bash
RUN_INTEGRATION_TESTS=1 pytest -m integration
```

That test should take at least 30 seconds and then show the transaction as `PROCESSED`.

What the tests cover:

| Check | What you should see |
| --- | --- |
| Health | HTTP 200, `status` is `HEALTHY`, time ends with `Z` |
| Valid webhook | HTTP 202 and `"status": "accepted"` |
| Bad payload | HTTP 422 for missing fields, a non-positive amount, a bad currency, or more than two decimal places |
| Unknown id | HTTP 404 and `"detail": "Transaction not found"` |
| New transaction | `PROCESSING`, and `processed_at` is `null` |
| Finished processing | `PROCESSED`, and `processed_at` is set |
| Duplicate webhook | HTTP 202 `"duplicate"`, one transaction row, one outbox row |
| Eight concurrent duplicates | One transaction, one outbox row, and one enqueued job |
| Failed attempt, then retry | Stays `PROCESSING` after the failure, then completes once. Another delivery does not call the processor again |
| Two workers at the same time | The second one leaves the row with the worker that holds the lease |
| Expired lease | A later worker can finish the row |
| Redis publish failure | The transaction stays saved as `PROCESSING`, the outbox returns to `PENDING`, and a later poll enqueues it |
| Database commit failure | Nothing is stored. A storage error on the webhook returns HTTP 503 |
| Response time | The webhook returns in under 500ms while the configured delay is 30 seconds |

Lint:

```bash
ruff check app tests alembic
```

The automated tests do not start PostgreSQL or Redis. `FOR UPDATE SKIP LOCKED` only runs when the database dialect is PostgreSQL, so the SQLite suite does not exercise that line. A manual check with `docker compose up` is the way to see the real database, queue, and 30-second delay together.

## Deployment

The web service is deployed on Render:

https://transaction-webhook-service-2.onrender.com

```bash
curl -sS https://transaction-webhook-service-2.onrender.com/
curl -sS -D - -H 'Content-Type: application/json' \
  -d '{"transaction_id":"txn_abc123def456","source_account":"acc_user_789","destination_account":"acc_merchant_456","amount":1500,"currency":"INR"}' \
  https://transaction-webhook-service-2.onrender.com/v1/webhooks/transactions
curl -sS https://transaction-webhook-service-2.onrender.com/v1/transactions/txn_abc123def456
```

A check on 20 October 2026 returned HTTP 202 for that webhook and stored the row as `PROCESSING`. `processed_at` was still `null` about a minute later. The status becomes `PROCESSED` only after the Celery worker and the outbox dispatcher are running with the same `DATABASE_URL` and `REDIS_URL` as the web service. The free web service also sleeps when it is idle, so the first request after that can take about a minute.

The steps below are how that Render service was set up, and how to add the worker and dispatcher.

You need five pieces:

1. Web process for the API image.
2. Worker process for Celery, listening on the `transactions` queue.
3. Worker process for `python -m app.workers.dispatcher`.
4. PostgreSQL.
5. Redis.

The image entrypoint runs `alembic upgrade head` and then the process command. Use `GET /` as the health check. Leave `PROCESSING_DELAY_SECONDS` at 30 if you want the same behavior as the assessment.

`render.yaml` describes those resources for Render. Render's blueprint format changes, so read the file before you apply it. The dashboard steps below are the fallback.

1. Push this project to a Git repository Render can read.
2. In Render, choose **New**, then **Blueprint**, and select the repo.
3. Confirm the database, Redis, web service, Celery worker, and dispatcher.
4. Deploy. The health check path is `/`.
5. The web service URL for this project is https://transaction-webhook-service-2.onrender.com. Call it with the curl commands at the top of this section.

You can also create the services by hand:

1. Create PostgreSQL and Redis.
2. Create a web service from the Dockerfile. Health check path: `/`.
3. Set `DATABASE_URL` and `REDIS_URL`. A `postgres://` URL is accepted.
4. Set `CELERY_TASK_QUEUE=transactions` and `PROCESSING_DELAY_SECONDS=30`.
5. Create a background worker with the same image and this command:

```bash
celery -A app.workers.celery_app:celery_app worker --loglevel=info -Q transactions
```

6. Create another background worker:

```bash
python -m app.workers.dispatcher
```

7. Give both workers the same database, Redis, and processing settings as the web service.

On a Linux VM with Docker:

```bash
git clone <your-repository>
cd transaction-webhook-service
docker compose up --build -d
```

Change the database password in `docker-compose.yml` before the host is reachable from the internet. Put TLS in front of port 8000 if the API is public. If you use a managed database, point `DATABASE_URL` and `REDIS_URL` at those hosts and do not publish the database ports.

## Project layout

```text
app/main.py                  FastAPI app
app/api/routes/              HTTP routes
app/core/                    Settings, time, and errors
app/db/                      Engine, sessions, money and time types
app/models/                  Transaction and outbox tables
app/schemas/                 Request and response models
app/repositories/            SQL used by the services
app/services/                Accepting webhooks, processing, and dispatch
app/workers/                 Celery app, task, and outbox poller
alembic/                     PostgreSQL migration
tests/                       Pytest suite
Dockerfile                   Image used by Compose and deployment
docker-compose.yml           Local Postgres, Redis, API, worker, dispatcher
render.yaml                  Render service definitions
.env.example                 Sample environment for running on the host
```

## Limitations

- Webhooks are not signed or authenticated. Anyone who can reach the endpoint can submit a transaction.
- The lookup endpoint is also public. That is fine for this assessment and not fine for a real payment API.
- Processing is `time.sleep`. It does not call Razorpay or any other payment API.
- A job can be delivered more than once. The status change is guarded, so the stored row is not completed twice. The sleep can still run twice if the worker dies after the sleep and before the commit, once the lease expires.
- The dispatcher polls. A transaction waits for the next poll, and then for the 30-second sleep.
- Tests run on SQLite. They do not prove the PostgreSQL row lock or a live Redis broker.
- After Celery's five retries, the outbox keeps publishing the same unfinished transaction. A bug in processing will be retried until the code or the data changes.
- There is no metrics dashboard, dead-letter screen, or autoscaling setup.
- The Render web service is live at https://transaction-webhook-service-2.onrender.com. Background processing still depends on the worker and dispatcher services staying up. The free instance sleeps when idle.
