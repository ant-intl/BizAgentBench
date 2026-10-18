# BizAgentBench

BizAgentBench evaluates agents on 50 payment-service tasks through multi-turn conversations and tool use. It reports Goal (G), Process (P), Protection (F), and Strict success using deterministic checks of business states and execution histories.

The task data, knowledge documents, simulated database, tools, and scoring rules are included. To run an evaluation, configure two models: the **agent** being tested and a **user simulator** that supplies customer information and approvals. Both roles can use a hosted API or a self-hosted model server.

## Installation

Use Python 3.12 and run the following commands from the repository root:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp configs/model.example.json configs/model.json
```

## Configure the two models

Edit both the `agent` and `user` sections in `configs/model.json`. Each endpoint must support OpenAI-compatible Chat Completions, streaming, and native tool calling. The roles may use different models, servers, and credentials.

| Field | Meaning |
| --- | --- |
| `base_url` | API base URL, typically ending in `/v1`. The runner appends `/chat/completions`. |
| `model` | Model identifier accepted by that endpoint. |
| `api_key_env` | Environment variable containing the credential for this role. |
| `max_tokens` | Output limit for an individual model response; set it to a value supported by the provider. |
| `timeout_seconds` | HTTP timeout for an individual network operation, including waiting for the next stream chunk. |
| `extra_body` | Provider-specific request parameters, such as reasoning settings. |
| `response_model` | Optional model identifier returned by the provider when it differs from the requested `model`. |

HTTPS APIs and local HTTP services at `127.0.0.1` or `localhost` are supported. For example, a locally served model may use `http://127.0.0.1:8000/v1` and its configured served-model name. For a remote model server, use an HTTPS endpoint or forward its port to localhost.

Set the credentials in your shell:

```bash
export AGENT_API_KEY='your-agent-key'
export USER_API_KEY='your-user-key'
```

For a local server with authentication disabled, set the corresponding variable to a non-empty placeholder such as `local`. Enable the server's tool-calling support and use its exact served-model name. Credentials stay in environment variables; model configurations and run outputs are ignored by Git.

The default user simulator is **DeepSeek-V4.1-Flash**. The example configuration uses the per-response output limits from the paper's DeepSeek-V4.1-Flash experiments:

| Role | Model in the example | `max_tokens` |
| --- | --- | ---: |
| Agent | Set `AGENT_MODEL_ID` to the model being evaluated. | 262,144 |
| User simulator | DeepSeek-V4.1-Flash (`deepseek-flash`) | 393,216 |

These are limits for a single response, not context-window sizes or cumulative token budgets. For other models, adjust `max_tokens` to the supported output limit. The official DeepSeek API uses `deepseek-flash` as the model identifier for DeepSeek-V4.1-Flash, matching the saved paper runs; other providers may use a different identifier. Set reasoning options explicitly in `extra_body` when the provider requires them.

The template sets a 5,400-second episode timeout and a tool-error threshold of 16, configurable through `episode_timeout_seconds` and `max_errors`. The error threshold counts rejected business calls and tool-execution errors across both roles; `tool_errors` reports this same count. There is no tool-call or dialogue-step cap. Context overflow can trigger compaction of older retrieval responses; the saved execution record remains complete.

The episode deadline is shared by both models and includes in-flight requests and retry delays. At the deadline, streaming connections are cancelled and partial logs are preserved. The run is recorded as `wall_clock_limit` and scored from the business state reached, using the same G/P/F checks.

## Run an evaluation

First, test the configuration on one task:

```bash
python tools/evaluate.py \
  --config configs/model.json \
  --output runs/smoke \
  --numbers 1 \
  --workers 1
```

Then run all 50 tasks once:

```bash
python tools/evaluate.py \
  --config configs/model.json \
  --output runs/model-run-1 \
  --workers 4
```

Run a subset by its published task numbers:

```bash
python tools/evaluate.py \
  --config configs/model.json \
  --output runs/subset-run-1 \
  --numbers 1 2 3 4 5 \
  --workers 4
```

`--workers` accepts values from 1 to 50. Choose concurrency according to the limits of both model endpoints. Each output directory must be new unless using `--resume`.

For three repetitions, use a separate output directory for each run:

```bash
for run in 1 2 3; do
  python tools/evaluate.py \
    --config configs/model.json \
    --output "runs/repeat-${run}" \
    --workers 4
done
```

The runner writes a separate summary for each repetition.

## Resume an interrupted evaluation

To resume an interrupted run:

```bash
python tools/evaluate.py \
  --config configs/model.json \
  --output runs/model-run-1 \
  --workers 4 \
  --resume
```

Use the same configuration, selected task numbers, and code/data assets when resuming. Cases with a saved `result.json` are skipped. For an incomplete case, cached responses are reused only when the saved request hash matches, reconstructing the existing execution before continuing.

## Results and scoring

Read `runs/model-run-1/summary.json` for the aggregate results. `paper_counts` contains passing counts and `paper_rates` contains rates from 0 to 1.

| Metric | Meaning |
| --- | --- |
| Goal (`G`) | Requested business outcomes and permitted terminal conditions. |
| Process (`P`) | Requirements governing the execution of business operations. |
| Protection (`F`) | Protected objects, ledger integrity, and prohibited side effects. |
| Strict | All three dimensions pass: `G and P and F`. |
| Intermediate failure | A process or protection requirement fails: `not (P and F)`. |

The verifier operates on business states and ordered execution histories, without an LLM judge. Different valid strategies can pass the same checks. The environment reward, per-task result, and summary all use the same G/P/F checks. In each verification result, `checks` and `failed_by_category` contain the scored predicates; `source_checks` retains their underlying evidence. `paper_metrics` is a copy of the same G/P/F result for reporting compatibility.

To print the scores:

```bash
python - <<'PY'
import json
from pathlib import Path

summary = json.loads(Path("runs/model-run-1/summary.json").read_text())
print(f"Finished: {summary['finished']}/{summary['planned']}")
print(f"Evaluated: {summary['evaluated']}")
for metric, rate in summary["paper_rates"].items():
    print(f"{metric}: {rate:.1%}" if rate is not None else f"{metric}: unavailable")
print("Run classifications:", summary["classifications"])
PY
```


The following files support inspection of individual runs:

| File under the output directory | Contents |
| --- | --- |
| `summary.json` | Aggregate scores, run classifications, and per-task results. |
| `experiment.json` | Model configuration, selected tasks, and hashes of code, data, simulator prompt files, and dependency declarations. |
| `case_NNN__trial0/result.json` | Task score, failed checks, termination reason, tool counts, and token usage. |
| `case_NNN__trial0/trajectory.json` | Completed conversation and tool history. |
| `case_NNN__trial0/final_world.json` | Final business state and execution history used for scoring. |
| `case_NNN__trial0/progress.json` | Progress while an episode is running. |

Interrupted or failed episodes may have `trajectory_live.json` or `trajectory_partial.json` instead of a completed trajectory. Inspect `result.json` and `provider_retries.jsonl`, when present, to distinguish model outcomes from provider or transport failures.

## Troubleshooting

- **Missing environment variable or HTTP 401:** check the variable named by `api_key_env` for the affected role.
- **Model not found or `PROVIDER_MODEL_MISMATCH`:** check the requested model ID and, if needed, set `response_model` to the provider's reported ID.
- **Invalid tool-call responses:** confirm that the model and server support native tool calling; a text-only completion endpoint is insufficient.
- **Output truncation:** inspect `output_limit_roles` in the task result and use a larger supported `max_tokens` value in a new run.
- **Resume rejected:** use the same model configuration, selected tasks, and code/data assets as the original run.

To check the local installation without calling model APIs:

```bash
python -m unittest discover -s tests -v
```

## License

The BizAgentBench evaluation code in `payment/runtime/` and `tools/` is released under the [MIT License](LICENSE).

The vendored Tau2 components retain their original [MIT license](third_party/tau2-bench/LICENSE).

## Citation

If you use BizAgentBench in your research, please cite our [paper](https://arxiv.org/abs/2610.20424):

```bibtex
@misc{zhao2026bizagentbench,
  title = {{BizAgentBench}: Unveiling Hidden Challenges in Deploying Long-Horizon Agents for Real-World Business},
  author = {Enhan Zhao and Yongqi Tong and Xuejun Yu and Hang Wang and Pan Wang and Yichao Wang and Di He and Jianshe Li and Wei Wu},
  year = {2026},
  eprint = {2610.20424},
  archivePrefix = {arXiv},
  primaryClass = {cs.CE},
  url = {https://arxiv.org/abs/2610.20424}
}
```
