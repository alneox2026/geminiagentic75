# Managed Agents Middleware Template (Google Antigravity & Gemini Managed Agents)

Production-ready, highly scalable middleware template for **Google Gemini Managed Agents** and **Google Antigravity Agents** (`antigravity-preview-09-2026` and custom managed agents).

This middleware serves as the unified, secure API gateway between client applications (FlutterFlow, Web, Mobile) and Google's hosted agent sandboxes, providing **real-time SSE streaming**, **atomic wallet billing**, **spend protection**, and **stateful multi-turn memory**.

## Billing preflight invariant — keep this order

Treat this ordering as a hard billing and side-effect boundary:

- Authenticate and validate existing thread/session ownership using server-owned state only.
- Reserve wallet credit. If reservation fails (including HTTP 402), return without creating a
  provider-side session or sending agent/model input.
- Only after reservation succeeds may the gateway create a provider-side session or submit
  agent/model input.
- If session creation fails after reservation, release the reservation idempotently.
- Keep buffered and streaming paths aligned; test that zero-credit requests return 402 with no
  provider-session or agent calls.

Purely local ID generation may happen before reservation only when it has no provider-side side effects.

## Delayed Gemini usage: recover and bill without blocking

The Gemini Interactions API may omit usage from the original response even when
it later exposes usage on `GET /v1beta/interactions/{interaction_id}`. The
gateway records the provider interaction ID with the completed turn. If the
worker cannot price a turn immediately, it releases that turn's temporary
wallet hold and marks the usage for reconciliation. The worker retries the
provider lookup with exponential backoff. A late recovered charge is debited
from then-available credit; any unpaid remainder stays recorded for billing
review. Missing usage does not block a follow-up turn: each new request is
rejected only when currently available credit cannot cover its new reservation.
If usage is still unavailable after the configured retention period, the turn
moves to manual billing review while the user may continue spending available
credits. Releasing the temporary hold does not mean treating missing usage as
zero cost or forgetting the outstanding charge.

An expired reservation with no completed-turn ledger is also ambiguous: it may
mean persistence is delayed or a completion event was lost, not that the model
definitely did no work. The worker releases the temporary wallet hold while
placing the possible charge into manual billing review. The missing-ledger
record remains visible for operators, but it does not reserve spendable credit
or block a new request by itself. A new request is rejected only when currently
available credit cannot cover its reservation.

Keep `billing_reconciliation_enabled = true` whenever prepaid wallet
enforcement is enabled. Terraform rejects an enforced deployment without its
reconciliation scheduler. Defaults are a one-hour scheduler interval, a
five-minute initial lookup backoff, a six-hour maximum backoff, and a seven-day
automatic lookup window. Tune these through `infra/terraform/terraform.tfvars`:

- `billing_reconciliation_schedule`
- `billing_usage_reconciliation_initial_backoff_seconds`
- `billing_usage_reconciliation_max_backoff_seconds`
- `billing_usage_reconciliation_retention_seconds`

After the retention window, retain the unresolved usage and manual-review
records; do not invent a zero-cost settlement or erase the possible charge.
The temporary authorization hold must already be released, and the pending
reconciliation count is informational only—it must never gate admission.

---

## 🌟 Key Features

1. **Native Gemini Interactions API Integration**:
   - Directly interfaces with `POST https://generativelanguage.googleapis.com/v1beta/interactions`.
   - Free environment compute (CPU/RAM Linux sandbox) during preview; bills only for underlying model tokens.
   - Supports only the canonical runtime model ID `gemini-3.8-flash`. Unknown versions, previews, moving aliases, and future models fail closed until separately priced and tested.

2. **Real-Time SSE Streaming**:
   - Instant TTFT (Time-To-First-Token) with low latency.
   - Streams autonomous execution deltas (`step.delta`), thought reasoning chunks, and final `interaction.completed` usage.

3. **Multi-Turn Stateful Continuity**:
   - Maps client conversation `thread_id` to Google's stateful interaction parameters (`previous_interaction_id` and `environment_id`).
   - The agent remembers previous turns, files generated inside `/workspace`, and bash script outputs across the entire chat thread.

4. **Accurate Token Normalization & Billing**:
   - Normalizes `total_input_tokens`, `total_output_tokens`, and `total_thought_tokens`.
   - Computes dollar-exact costs per turn and debits user wallets asynchronously via Pub/Sub.

5. **Provider Token Budget (`max_total_tokens`)**:
   - Sends the configured `max_total_tokens` limit to the managed-agent API.
   - This token limit does not guarantee against wallet shortfalls or non-token costs. The wallet hold is authorization only; final settlement uses reported usage, and provider limit behavior must be verified.

6. **Isolated Deployment Identity**:
   - Each cloned middleware receives its own Artifact Registry image names,
     Terraform state prefix, and Firestore collection namespace.
   - This prevents a new cluster from importing, overwriting, or deleting an
     existing middleware's resources.

7. **Production Cloud Run Optimization**:
   - Configured with `600s` timeout for deep multi-step agentic workflows.
   - Tuned concurrency (`16`) to eliminate stream jitter and CPU starvation.
   - Default `gateway_min_instances = 0` saves cost; for latency-critical production clusters with zero cold starts, set `gateway_min_instances = 1`.

---

## 📁 Project Structure

```
├── common/                  # Shared Pydantic schemas (AgentConfig, ChatRequest, Wallet)
├── config/
│   ├── agents.dev.yaml      # Development agent registry (Antigravity & custom agents)
│   ├── agents.prod.yaml     # Production agent registry
│   ├── billing.test.yaml    # Dated test rate schedule for gemini-3.8-flash
│   └── billing.prod.yaml    # Production pricing catalog
├── docs/
│   ├── ARCHITECTURE.md      # In-depth technical architecture
│   └── ADDING_MANAGED_AGENTS.md # Guide to registering new managed agents
├── infra/terraform/         # Cloud Run, Secret Manager, Pub/Sub, and Monitoring
├── scripts/
│   ├── cloudshell_build_middleware.sh  # Builds and pushes container images
│   └── cloudshell_deploy_middleware.sh # Applies Terraform deployment
├── services/
│   ├── agent_gateway_v3/    # Public API Gateway (FastAPI + SSE streaming)
│   └── agent_persistence_worker_v3/ # Background Pub/Sub worker for Firestore & ledger
└── tests/                   # Complete test suite (93 unit & integration tests)
```

---

## 🚀 Quickstart: Deploying a New Cluster

### Step 1: Create a New GitHub Repository
1. In your GitHub account, create a new repository (e.g. `v6stack` or `gemini-agents-stack`).
2. Copy the contents of this template into your new repository and push to `main`.

### Step 2: Set Your Gemini API Key in Secret Manager
In Google Cloud Shell:
```bash
echo -n "YOUR_GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
```

The Gemini API key has its own Google AI Studio billing/prepaid-credit balance.
It is separate from the user's SuperApp wallet: Stripe top-ups fund the app
wallet, not Gemini API credits. An upstream `402 payment_required` means the
Gemini API prepaid balance linked to this key is depleted; add Gemini API
credits (or enable auto-reload) in Google AI Studio before retrying.

### Step 3: Clone, Build & Deploy from Cloud Shell
```bash
# Clone your new stack. The directory name becomes the default stack identity.
cd ~
git clone https://github.com/YOUR_USER/agentic4.git
cd ~/agentic4

# On every first deployment or update, fetch exactly the committed revision.
git fetch origin main
git reset --hard origin/main

# Build only agentic4-prefixed container images.
bash ./scripts/cloudshell_build_middleware.sh

# Deploy into stacks/agentic4/middleware state with agentic4-scoped runtime
# collections and the configured shared billing collections.
bash ./scripts/cloudshell_deploy_middleware.sh
```

### Deployment identity and safety defaults

The Cloud Shell scripts derive `MIDDLEWARE_STACK_NAME` from the clone directory
(`agentic4` above). They use it for image names, Terraform remote state, and
as the namespace for agent-runtime Firestore collections. Shared billing
collections retain their Terraform-configured names. Set `MIDDLEWARE_STACK_NAME`,
`MIDDLEWARE_IMAGE_PREFIX`, or `FIRESTORE_NAMESPACE` only when you deliberately
need a different identity.

### Shared Central Billing vs. Stack-Scoped Collections

The Cloud Shell deploy script distinguishes between **shared central billing collections** and **stack-specific agent runtime data**:

1. **Shared Central Billing Collections**:
   - `FIRESTORE_CUSTOMER_WALLETS_COLLECTION`
   - `FIRESTORE_WALLET_TRANSACTIONS_COLLECTION`
   - `FIRESTORE_CUSTOMER_BILLING_PERIODS_COLLECTION`
   With `SHARE_CENTRAL_BILLING_COLLECTIONS=true` (default), the deploy script uses the exact values configured for the corresponding Terraform variables in `terraform.tfvars` or `variables.tf`; the template defaults are the canonical `_v3` names. Thus customized names such as `*_service43` are preserved and must match the central billing service exactly.

2. **Stack-Scoped Collections**:
   - `FIRESTORE_THREADS_COLLECTION` (`agent_threads_<namespace>`)
   - `FIRESTORE_MESSAGES_SUBCOLLECTION` (`messages_<namespace>`)
   - `FIRESTORE_IDEMPOTENCY_COLLECTION` (`processed_events_<namespace>`)
   - `FIRESTORE_BILLING_LEDGER_COLLECTION` (`agent_billing_ledger_<namespace>`)
   - `FIRESTORE_BILLING_RESERVATIONS_COLLECTION` (`billing_reservations_<namespace>`)
   These remain isolated per stack using `${FIRESTORE_NAMESPACE}` so multiple agent stacks in the same GCP project do not collide on thread sessions or turn reservations.

To customize shared collection names, set the corresponding Terraform variables in `terraform.tfvars` or `variables.tf`. The script validates any exported `FIRESTORE_*_COLLECTION` override against the configured Terraform value to catch stale Cloud Shell settings; `ALLOW_CUSTOM_SHARED_COLLECTIONS=true` is required to intentionally override it. With `SHARE_CENTRAL_BILLING_COLLECTIONS=false`, the wallet, transaction, and billing-period collections are stack-scoped by default; inherited environment overrides fail closed, and an explicit custom override requires the same opt-in. The other runtime collections remain stack-scoped as usual. Do not change `FIRESTORE_NAMESPACE` just to share selected billing records.

The deploy script does not import existing resources unless
`IMPORT_EXISTING_RESOURCES=true` is set for a reviewed recovery operation. It
also refuses a Terraform plan containing deletes or replacements unless
`ALLOW_TERRAFORM_DELETES=true` is explicitly set. Do not reuse an existing
stack's state prefix or Firestore namespace for a new middleware.

The upstream read timeout (`upstream_read_timeout_seconds`, default `570s`) limits idle time between chunks; it is not a total-turn limit. The gateway also enforces an absolute wall-clock deadline (`upstream_total_timeout_seconds`, default `570s`, measured from request start) so a continuously streaming agent cannot run past the Cloud Run `600s` deadline. Keep any override lower than `GATEWAY_TIMEOUT`; Terraform rejects a non-positive total timeout or one that is not below the gateway timeout. The 30-second cushion is an upper bound for cleanup because request setup and response delivery also consume Cloud Run time. On any post-acceptance timeout or error, the gateway does **not** release the reservation directly; it publishes the partial turn and accumulated usage metadata to the persistence worker via Pub/Sub for settlement and reconciliation. Only confirmed pre-request failures (connection refused, connect timeout) release the hold immediately.

The billing catalog automatically changes from introductory to standard Gemini 3.8 Flash rates on January 1, 2027 (UTC), and records the selected `pricing_version` and `pricing_effective_from` on each usage record. Review configured reservation holds before that date; holds are not spending caps. The token catalog prices cached input tokens but does not meter explicit cached-content storage time or grounding requests; do not enable those billable features for customer cost recovery without adding their usage metering.

Terraform owns the billing-reservation composite index, so use the Cloud
Shell deployment script rather than `firebase deploy --only firestore:indexes`
for this template.

### Production deploy safeguards

The Cloud Shell deploy script defaults to `DEPLOYMENT_ENV=development` and
asks for an explicit `yes` after showing a Terraform plan. For production, set
`DEPLOYMENT_ENV=production` and provide a non-empty
`ALERT_NOTIFICATION_CHANNELS_JSON` value (a JSON array of Cloud Monitoring
channel resource names). Production mode enables Cloud Run deletion
protection and refuses the plan if known development agent targets remain
unless `PRODUCTION_AGENT_TARGETS_REVIEWED=true` is deliberately set after
review. Automated applies require the explicit `TERRAFORM_AUTO_APPROVE=true`
opt-in. Reconciliation defaults to hourly; restore `*/15 * * * *` when the
production operating cadence calls for 15-minute processing.

---

## ⚙️ Configuration: Registering Agents

In `config/agents.prod.yaml`:
```yaml
agents:
  # Base Antigravity Agent
  antigravity_agent:
    agent_id: antigravity_agent
    backend: gemini_managed
    remote_agent_id: antigravity-preview-09-2026
    model: gemini-3.8-flash
    max_total_tokens: 50000
    streaming_enabled: true
    persistence_enabled: true

  # Custom Managed Agent (created via API or Google AI Studio)
  data_analyst:
    agent_id: data_analyst
    backend: gemini_managed
    remote_agent_id: data-analyst
    model: gemini-3.8-flash
    max_total_tokens: 50000
    streaming_enabled: true
    persistence_enabled: true
```

---

## 🚀 Production Go-Live Checklist

Follow this checklist before approving and deploying a production middleware stack:

1. **GCP Project Isolation**:
   * Set your active production Google Cloud Project:
     ```bash
     gcloud config set project <PRODUCTION_PROJECT_ID>
     export PROJECT_ID="<PRODUCTION_PROJECT_ID>"
     ```
   * Confirm that the deploy script banner prints `<PRODUCTION_PROJECT_ID>` and not `ceo-dev123`. Do not use `ALLOW_DEV_PROJECT_IN_PROD=true`.

2. **Browser Origins (CORS)**:
   * Export your exact production domain(s) as a plain string without markdown formatting:
     ```bash
     export ALLOWED_ORIGINS="https://app.yourdomain.com"
     ```

3. **Production Cloud Monitoring Alerts**:
   * Production deployments strictly require at least one Cloud Monitoring notification channel:
     ```bash
     export ALERT_NOTIFICATION_CHANNELS_JSON='["projects/<PRODUCTION_PROJECT_ID>/notificationChannels/<CHANNEL_ID>"]'
     ```

4. **Agent Backends & Target Review**:
   * In `config/agents.prod.yaml`, review and replace any dev targets with verified production remote agent configurations.
   * Do not set `PRODUCTION_AGENT_TARGETS_REVIEWED=true` without actually verifying the targets.

5. **Manual Terraform Review**:
   * Ensure `TERRAFORM_AUTO_APPROVE` is unset or `false`.
   * Manually review the output plan in Cloud Shell, verify resource names and IAM roles, then type `yes` when prompted.

6. **Model Pricing and Reservations**:
   * Confirm every agent uses exactly `gemini-3.8-flash`; unlisted aliases and model versions intentionally fail closed.
   * Confirm the effective-dated rate schedule and review reservation amounts against the standard rates before January 1, 2027.

---

## 🧪 Running Tests Locally

```bash
python -m pytest
```
All unit and integration tests should pass.
