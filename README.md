# Compliance Review Agent

A LangGraph agent that reviews commercial contracts for a UK regulated firm. It
extracts clauses, scores their risk, pauses for a human to approve, edit or
reject high-risk findings, drafts remediation notes, and produces an auditable
report.

It's built to show the parts of an LLM system that matter in production:
durable state, human-in-the-loop control, observability, evals, and an
automated, secure deployment to AWS. **Every part of it runs for £0** (see
[Running it for free](#running-it-for-free)).

```
                    ┌──────────────────────────────────────────────────────────┐
POST /reviews ───▶  │ extract_clauses ─▶ score_risk ─┬─▶ human_review ──┐      │
                    │                                │   (interrupt)    ▼      │
                    │                                └──────────▶ draft_remediation ─▶ finalize
                    └───────────────────────┬──────────────────────────────────┘
                                            │ checkpoint after every node
                                            ▼
                     PostgreSQL               Ollama (open-weights model)        Langfuse
                     durable state            local / sidecar, $0                traces, tokens, cost, eval scores
```

## How it works

**State.** `app/state.py` defines a typed `ReviewState` holding the document,
extracted `Clause`s, `Finding`s (Pydantic models), the final report, and two
append-only lists, `metrics` and `audit_log`, that every node adds to through
`operator.add` reducers.

**Checkpointing.** The graph is compiled with LangGraph's `PostgresSaver`, so
state is saved after every node. A paused review survives restarts and deploys,
any ECS task can resume any thread, and `GET /reviews/{id}/history` returns the
full checkpoint trail for audit. Deserialisation is restricted to the app's own
types through an allow-list.

**Human in the loop.** After scoring, any finding at or above
`REVIEW_THRESHOLD` routes to `human_review`, which calls `interrupt()`. The run
stops and the API returns the findings that need a decision. A reviewer posts
decisions to `/reviews/{id}/decisions`, and the graph resumes with
`Command(resume=...)`:

- `approve`: keep the finding and draft a remediation
- `edit`: change the risk level, add a note
- `reject`: mark it as a false positive and skip remediation

Partial answers are allowed. The node interrupts again for whatever is still
pending. Every decision is written to the audit log with the reviewer's name.

**Models.** `LLM_PROVIDER` picks the analyzer:

| Provider    | What it is                                                | Cost |
|-------------|-----------------------------------------------------------|------|
| `rules`     | Deterministic baseline; default, used by unit tests       | £0   |
| `ollama`    | Open-weights model (Qwen 2.5) served by Ollama            | £0   |

Both share one interface, prompts and Pydantic schemas, so the eval
compares them like for like.

**Observability.** Two layers:

- *Langfuse* traces every graph node and LLM call (prompt, output, tokens,
  latency), grouped by session = review thread and tagged with the reviewer.
  Eval scores are attached to the same sessions.
- *In-state metrics*: each node records latency, input and output tokens, and
  cost. With a local model the real cost is $0. Set `INPUT_/OUTPUT_COST_PER_MTOK`
  to an API's list price to report what the same workload would cost there,
  which gives you a concrete "self-hosting saves $X per 1,000 reviews" figure.

CloudWatch covers the infrastructure layer (see `infra/terraform/monitoring.tf`).

**Evals.** `evals/golden/contracts.json` holds labelled contracts.
`python -m evals.run_evals` runs each one through the full graph (auto-approving
the interrupt) and reports risk accuracy, high-risk recall and precision, cost
per document, and mean and p95 latency. `--min-high-recall` turns it into a CI
gate.

## Run it locally

```bash
pip install -r requirements-dev.txt
pytest -q                          # 11 tests, offline (includes a fake Ollama server)
python -m evals.run_evals          # rule baseline

# With a real model (install Ollama from ollama.com first):
ollama pull qwen2.5:7b             # or qwen2.5:3b on a laptop without a GPU
LLM_PROVIDER=ollama python -m evals.run_evals

# Full stack: API + Postgres + Ollama in containers
cp .env.example .env
docker compose up --build
```

```bash
curl -X POST localhost:8000/reviews -H 'content-type: application/json' \
  -d '{"doc_id":"msa-1","document_text":"1. The Vendor may suspend the Services without notice.\n\n2. Fees are payable within 30 days."}'
# -> {"status":"awaiting_review","thread_id":"...","pending":{...}}

curl -X POST localhost:8000/reviews/<thread_id>/decisions -H 'content-type: application/json' \
  -d '{"reviewer":"owen","decisions":[{"clause_id":"c1","action":"edit","risk":"medium","note":"OK with 24h notice"}]}'
# -> {"status":"completed","report":{...}}
```

## CI/CD

```
git push ─▶ GitHub Actions CI: ruff ─▶ pytest ─▶ eval gate ─▶ pip-audit ─▶ terraform validate ─▶ docker build
                │                └─▶ (main) LLM eval: Ollama + qwen2.5:3b on the runner, results in the job summary
                ▼ (main only, after manual approval on the "production" environment)
           Deploy: GitHub OIDC ─▶ build ─▶ push to ECR ─▶ new task def revision ─▶ ECS Fargate rolling deploy
                                                                                    │ circuit breaker auto-rollback
                        ALB ─▶ ECS task [ api + ollama sidecar ] (Fargate Spot) ─▶ RDS Postgres
                                          │
                        Secrets Manager ─▶ injected at runtime
                        CloudWatch ─▶ logs, 5xx / p95 / health alarms ─▶ SNS
```

## Running it for free

| Piece                 | Free option                                                                 |
|-----------------------|-----------------------------------------------------------------------------|
| LLM                   | Ollama + Qwen 2.5 on your machine, the CI runner, or as an ECS sidecar      |
| Postgres              | Docker locally; RDS on AWS (covered by free-plan credits)                   |
| Langfuse              | Cloud Hobby plan: free, no credit card, 50k units/month                     |
| CI/CD                 | GitHub Actions is free for public repos                                     |
| AWS                   | A **Free plan** AWS account: never charges you (details below)              |

### AWS without paying anything

Open a new AWS account and pick the **Free plan**, not the Paid plan. On the
Free plan AWS doesn't charge you at all. You get $100 of credits on sign-up and
up to $100 more for completing onboarding activities. The plan lasts 6 months
or until the credits run out, whichever comes first. After that the account is
suspended (nothing is billed), and you can upgrade within 90 days to keep it.

Rules to stay at £0:

1. **Don't upgrade to the Paid plan.** That's the only way charges can start.
2. **Keep it scaled to zero.** `desired_count` defaults to 0. Scale up for a
   demo, then scale back down, or `terraform destroy` the whole thing. RDS and
   the ALB use credits while they exist even with no tasks running, so destroy
   between demos.
3. Set `alarm_email` to get a budget alert (threshold $1) as a backstop.
4. Check in the console that ECS, RDS and Secrets Manager are available on your
   Free plan account before applying. AWS restricts a few services on the Free
   plan and doesn't publish a simple list.

Credit-saving choices already made in the Terraform: no NAT gateway (the
biggest fixed cost), Fargate Spot, one task, `db.t4g.micro`, a 1.5B-parameter
model, 30-day log retention.

### Deploy steps

1. `cd infra/terraform && terraform init && terraform apply -var github_repo=<you>/compliance-review-agent -var alarm_email=<you>`
2. Optional: fill the two Langfuse secrets listed in the `app_secret_names` output:
   `aws secretsmanager put-secret-value --secret-id compliance-review/langfuse-public-key --secret-string pk-...`
3. In GitHub, create a `production` environment with yourself as a required
   reviewer, and add the `github_deploy_role_arn` output as its
   `AWS_DEPLOY_ROLE_ARN` secret.
4. Push to main and approve the deployment. This pushes the image.
5. Demo: `terraform apply ... -var desired_count=1`, wait for the model pull
   (~3–5 min), hit the `api_url` output.
6. Done: `terraform destroy`.

## Still to do before this goes on the CV

- [ ] Run the evals with Ollama (`qwen2.5:7b` locally) and record the real
      numbers against the rule baseline: accuracy, high recall, p95 latency.
- [ ] Grow the golden set. 8 contracts / 32 clauses is a starter; aim for 30+
      contracts so the metrics mean something.
- [ ] Create a Langfuse Hobby project, run a few reviews, and screenshot a
      trace for the README.
- [ ] Deploy to AWS once end to end on a Free plan account, so you've seen
      the pipeline, OIDC and a rollback actually work. Then destroy it.
- [ ] Add the SageMaker classifier as a pre-filter node (project 2).
