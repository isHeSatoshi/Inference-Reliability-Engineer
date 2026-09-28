# Inference Reliability Engineer

An agent that owns the reliability of an inference deployment, built on [TrueForge](https://trueforge.dev). It works out what changed, reproduces the failure somewhere safe, experiments until it has a fix that measurably works, and stops to ask before it restarts anything a person is currently using.

## The problem

A model deployment degrades in ways that never show up as errors. Requests get slower and slower while the error rate stays at zero. The cache fills and the scheduler starts holding requests back instead of failing them, so the queue grows and the latency curve bends. A prefix that used to be cached is re-read on every call. Someone raises a batch size to claw back throughput and the incident gets worse.

None of that looks broken from a dashboard, and nobody owns it the way an SRE owns uptime. Latency SLOs breach, goodput falls, and the first question is which of a dozen things changed, most of which did not.

The agent answers that question by measurement. It reads the deployment's own metrics, logs, request log and change history, works out what actually moved, reproduces the failure on a second machine, and tests fixes until one is proven or it runs out of ideas and says so.

## What it covers

| | |
|---|---|
| Latency | time to first token and end to end, against SLOs |
| Goodput | the share of requests that succeed *and* arrive in time |
| Capacity | when the serving tier runs out of cache or concurrency and starts queueing |
| Config | which knobs are safe to change, in what range, and what that does to the rest |
| Change history | whether anything was deployed, or whether the traffic moved instead |
| Quality | evals against a golden set, required before precision changes land |

It only changes serving configuration. The served model is off limits, because swapping it changes what users get and that is a product decision.

## How it works

```
triage → what changed → classify → reproduce on shadow → experiment
       → prove → plan → approval → apply → verify
```

The agent forms hypotheses and tests them rather than recalling defaults. It writes its own analysis code in a credential-less sandbox, so a number it quotes is one it computed rather than one it was told. Each hypothesis becomes an experiment on the shadow machine, replaying traffic captured from production. A hypothesis that does not beat the baseline is dropped on the numbers, and the next one runs.

When nothing passes, it changes nothing and says what it tried. In one recorded run it discarded six single-lever experiments before finding a combination that held, and it withheld the change for two hours while the operator extended the search.

## When it stops and when it doesn't

The rule is one line: anything reversible runs freely, anything that interrupts a human's traffic stops and asks.

Telemetry, logs, config reads and shadow experiments have no gate. Gating them would only teach people to approve without reading. `apply_production_config` and `rollback_production` restart the server serving live requests, so both need approval, and the card carries the diff, the evidence run, the justification, the blast radius and the expected before and after numbers.

The evidence rule is enforced server-side, so it holds even when the model is wrong and even after a human approves. A production change is refused unless a shadow run of that exact config hash passed every SLO on production traffic captured in the last 90 minutes, at 1x or better of production rate, for 45 seconds or more. Precision changes need a quality eval first. Only allowlisted knobs are settable, within safe ranges.

If the new engine fails its health check after a restart, it rolls itself back.

## Proof it works

One incident, run end to end on real hardware. This is a worked example, not the product.

Qwen2.5-7B-Instruct on vLLM 0.30, one NVIDIA L4 per node, 0.6 requests per second held constant throughout. Short chat traffic was healthy for weeks. Then product started sending long RAG contexts and p95 latency went to 28 seconds with the error rate still at zero. No config change, no model change, nothing in the logs.

The cause was in the physics. The weights take about 15 GB of a 24 GB card, leaving a KV cache of roughly 52,000 tokens. At 5,000 to 11,000 tokens per prompt that fits a handful of concurrent requests, and the config admitted 256. The cache filled, requests waited for capacity, and prefix caching was off so the shared system prompt was prefilled again every time.

| | Healthy (~5% long prompts) | Incident (~50% long prompts) |
|---|---|---|
| p95 TTFT | 2.5 s | 21.9 to 28.7 s |
| p95 end to end | 20.9 s | 137 to 163 s |
| output tokens/s | 147 | 7 |
| KV cache usage, mean / max | 5% / 22% | 81% / 99% |
| running / waiting for KV capacity | 11 / 0 | 63 to 95 / up to 15 |
| error rate | 0% | 0% |

The fix, proven on the shadow L4 with captured incident traffic over 60 second replays:

| Config | p95 TTFT | p95 e2e | Goodput | Verdict |
|---|---|---|---|---|
| production (`max_num_seqs: 256`, no prefix caching) | 14.8 s | 54 s | 41% | the incident |
| `max_num_batched_tokens: 16384` | 15.6 s | 68 s | 15% | worse |
| `max_num_seqs: 16` + prefix caching | 6.8 s | 28 s | 100% | the fix |
| the same + `kv_cache_dtype: fp8` | 6.7 s | 30 s | 100% | also works, needs the quality gate |
| `max_num_seqs: 16` + prefix caching at 1.0 rps | 9.4 s | 47 s | 74% | hits the card's capacity limit |

SLOs in `mcp_server/policy.yaml`: p95 TTFT at or under 8 s, p95 end to end at or under 40 s, error rate at or under 1%, goodput at or above 90%. One 8,000 to 10,000 token prefill takes about 3 s on an L4, so tighter TTFT targets are not reachable with long-context traffic.

The second row is the one worth reading twice. Raising `max_num_batched_tokens` is what most people reach for, and on real hardware it made the incident worse. An agent that reasons from defaults would have shipped it.

That run used TrueForge on the control node, an OpenAI-hosted model, and a Daytona sandbox for the agent's analysis code. None of that is baked in. The recorded stack was:

| Piece | Used here | Swappable with |
|---|---|---|
| Model | OpenAI | any model your TrueForge instance has configured |
| Sandbox | Daytona, remote | any sandbox provider, or TrueForge's local mode |
| GPU nodes | Lightning AI Studios on GCP | AWS `g6.xlarge`, or any box the control node can reach |
| Metrics | Prometheus | point at your own, or let one start on the control node |

Screenshots of the full run: [docs/SCREENSHOTS.md](docs/SCREENSHOTS.md).

## Architecture

```
 laptop ──SSM tunnel──▶ CONTROL (AWS t3.large, ap-south-1; no public ports)
                        ├─ TrueForge :8790 (agent, approvals, UI) ──── sandbox
                        ├─ inference-ops MCP server :8765 (bearer token)
                        ├─ Prometheus :9090
                        └─ chaos tooling (operator only)
                               │ HTTPS + bearer token
            ┌──────────────────┴──────────────────┐
     ff-prod Studio (GCP, 1 x L4)          ff-shadow Studio (GCP, 1 x L4)
     controller :9000                      controller :9000
     vLLM 0.30 (127.0.0.1:8100)            vLLM 0.30 (127.0.0.1:8100) ← candidates
     loadgen = the users + request log     replay harness + quality eval
```

The shadow node is the point. It is the same engine, the same weights and the same image as production, and it is the only place a candidate config is ever allowed to exist. That is what makes an experiment's result worth believing, and it is why the GPU budget goes to a second box rather than to a bigger first one.

## Tools

`inference-ops` exposes 20 tools over streamable HTTP with bearer auth.

| Class | Tools |
|---|---|
| Observe, read only, no gate | `get_slo_status`, `compare_windows`, `get_request_log`, `get_engine_metrics`, `query_prometheus`, `get_serving_config`, `get_change_history`, `get_logs`, `get_gpu_status`, `get_policy`, `list_experiments`, `get_experiment`, `plan_production_change`, `wait_for_shadow` |
| Shadow, no gate, never touches prod | `capture_workload`, `deploy_shadow`, `run_load_test` (hypothesis required), `run_quality_eval` |
| Production, approval required | `apply_production_config`, `rollback_production` |

The tools return data, not answers. The traffic scenario name is never exposed to the agent.

## Setup

What the agent needs is a model and a sandbox, and it uses whatever your TrueForge instance already has. Add a model provider under Settings, add a sandbox provider if you want the agent to run its own analysis code, and point it at your deployment. Credentials live in your TrueForge instance, not in this repo.

Infrastructure:

```bash
AWS_REGION=ap-south-1 ./infra/aws/launch.sh
HF_TOKEN=hf_xxx ./infra/aws/secrets.sh
./infra/aws/deploy_code.sh
./infra/aws/ssm.sh prod    'tail -n 20 /var/log/firefighter-setup.log'
./infra/aws/ssm.sh shadow  'tail -n 20 /var/log/firefighter-setup.log'
./infra/aws/ssm.sh control 'tail -n 20 /var/log/firefighter-setup.log'
./infra/aws/tunnel.sh            # -> http://localhost:8790
./infra/aws/ssm.sh control '/opt/firefighter/infra/node/ops.sh register-agent <model-fqn>'
./infra/aws/ssm.sh control '/opt/firefighter/infra/node/ops.sh smoke'
```

Check the AWS quota first if you are using `g6` nodes. Service Quotas, EC2, "Running On-Demand G and VT instances" in `ap-south-1` must be at least 8 vCPUs for two `g6.xlarge` nodes. If the quota is not there, any other GPU host the control node can reach works, which is what the recorded run used:

```bash
export LIGHTNING_API_KEY=...
for r in prod shadow; do
  python -m infra.lightning.studio up $r
  python -m infra.lightning.studio push $r
  python -m infra.lightning.studio setup $r
done
python -m infra.lightning.studio logs prod setup    # wait for "SETUP COMPLETE (prod)"
./infra/aws/deploy_code.sh control
python -m infra.lightning.studio stop prod          # stop GPU billing when done
```

After a Studio restart, `python -m infra.lightning.studio start <role>` brings the controller, engine and traffic back.

Keys, by role rather than by vendor:

| Role | Where it goes | Required |
|---|---|---|
| Model provider key | your TrueForge instance, Settings, Models | yes |
| Sandbox provider key | your TrueForge instance, Settings, Sandbox providers | only if the agent should run code |
| Cloud credentials | your own laptop, for `infra/aws/*.sh`. Instances use an IAM role with no keys on them | yes, for the AWS path |
| Hugging Face read token | `HF_TOKEN=… ./infra/aws/secrets.sh` (SSM SecureString) | recommended, avoids download rate limits |
| GitHub fine-grained PAT | `GITHUB_TOKEN=… GITHUB_REPO=owner/repo ./infra/aws/secrets.sh` | optional, commits each approved config as an audit trail |
| Prometheus URL and bearer token | `PROMETHEUS_URL=… ./infra/aws/secrets.sh` | optional, one is started on the control node otherwise |
| `CONTROLLER_TOKEN`, `MCP_AUTH_TOKEN` | generated by `secrets.sh` into SSM and instance env files | automatic |

## Running it

Inject a scenario, then talk to the agent:

```bash
/opt/firefighter/infra/node/ops.sh chaos reset --clear-evidence /opt/firefighter/state/mcp
/opt/firefighter/infra/node/ops.sh chaos traffic long_context_shift
```

In TrueForge, start a chat with the agent:

> Production inference is degraded: p95 latency and goodput alerts are firing.
> Find the cause, prove a fix, and prepare the remediation.

Scenarios ship for a traffic shift to long contexts, a traffic surge, and a transient burst where the right answer is no change. `chaos/` is operator-only and the agent cannot reach it.

## Layout

```
agent/            instructions.md, create_agent.py (registers connector + agent)
skills/           incident-runbook, inference-engineering
mcp_server/       server.py (tools), policy.yaml (SLOs + evidence rules), evidence.py
controller/       per-GPU-node service: vLLM container, metrics, logs, replay, eval
workload/         dataset.py, loadgen.py, loadtest.py, scenarios/
common/           vllm_config.py (knob allowlist), stats.py, promtext.py
chaos/            operator-only scenario injection
infra/            aws/, node/, prometheus/, configs/
dev/              fake_vllm.py + run_local_stack.sh
tests/            unit tests + MCP smoke test
```

## Teardown

```bash
./infra/aws/teardown.sh          # terminate instances
./infra/aws/teardown.sh --all    # also SG, IAM role, code bucket, SSM parameters
```
