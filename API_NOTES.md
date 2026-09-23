# Oracle API contract

Verified against official documentation on 2026-09-20:

- [classifier.dev developer guide](https://classifier.dev/developers)
- [classifier.dev OpenAPI](https://classifier.dev/openapi.json)
- [TypeSafe HTTP API](https://docs.typesafe.ai/api)
- [TypeSafe confidence semantics](https://docs.typesafe.ai/confidence)

`classify_windows(windows, provider="classifier", batch_size=10, timeout=60, progress=None)` returns `predictions`, `requests`, and `errors`. Each window has a unique string `window_id` and preassembled `input` string. Only `input` goes to the service; metadata and ground truth stay local. The callback receives progress strings. Each request timeout applies to one HTTP attempt.

The `classifier` provider sends five independent single-label batches to `/v1/classify`, `tier=fast`, without any credential. Batches preserve input order. Published free limits are 3,000 decisions/minute and 20,000/day, counted per question per window; a million windows therefore represent five million decisions. Each input is limited to 32,000 UTF-16 units conservatively, each batch to 1,000 inputs. The default 10 stays below the documented 20-input fallback ceiling. The current API also supports a dimensions mode; this adapter uses the established label-batch shape.

The `typesafe` provider uses `TYPESAFE_API_KEY`, sends one trace as `state` and all five Choice questions together to `api.typesafe.ai/v1/systemone`, and requests `jev-latest`. It stores the returned resolved model, never assuming the alias is the version actually used. `batch_size` affects classifier only. Credentials are never forwarded to classifier or a redirect.

Answers retain raw `confidence`, `scores`, provider and model provenance. The optional `p_anomaly` is the sum of the model's SUSPICIOUS and FAULT probability mass. It is not an empirically measured fault probability. TypeSafe confidence measures distribution concentration; it is not the probability that a judgment is correct. The two quantities remain separate. Category is a trace-based hypothesis; normal-case severity is a MINOR placeholder to exclude from severity evaluation.

Q4 asks whether to continue with the next simulated test or pause SIL exploration to review a dangerous anomaly. Its answer is advisory only and never controls hardware or the runner. A probability sum above one is left unscored instead of silently capped; the raw scores remain available for inspection.

The classifier documentation uses inconsistent wording: its dimensions schema explicitly rejects interpreting confidence as literal correctness probability, while older single-result prose suggests that interpretation. Its [official implementation](https://github.com/mrmps/classifier-dev/blob/main/src/jev.ts) forwards TypeSafe confidence, rounded to four decimals; its gateway path can substitute the winning option probability when confidence metadata is absent. Preserve the value as reported and evaluate calibration on held-out motor traces before relying on it. Average confidence is not directly comparable across these paths.

Missing scores stay null. Fallback models remain named as returned, including `mixed` and per-result models. A prediction must not be described as a Jev result unless its model provenance supports that claim. `requests` records every HTTP attempt with measured latency, attempted decision count, reported usage, actual model when reported, and nullable cost. No token count, price, or cost is fabricated.

HTTP 429/5xx retries are bounded to three attempts with exponential backoff or Retry-After. Retry-After above 30 seconds and daily limits stop the run and leave remaining answers abstained. This avoids retrying before a quota reset. A local limiter counts all attempted classifications during one invocation; quotas shared by other processes remain enforced by the server. Validation failures, transport failures and incomplete result arrays never become NORMAL. IDs remain aligned, and malformed individual answers abstain without discarding valid peers. Live inference is explicitly initiated by the caller; tests mock the transport.

## Requirement quality review

`requirement_review.py` passes explicit, per-call `questions` and `context` keyword arguments to the shared adapter. It does not change the global five-question trace configuration. The eight axes are ambiguity, testability, atomicity, subject, trigger, completion, quantitative and safety. The first seven classify requirement quality; safety is a topic classification, not a quality defect. These are separate questions without an assumption of statistical independence.

For this workflow classifier batches use eight independent label sets, one request per axis per batch. Direct TypeSafe sends all eight Choice questions for a requirement together. The official API also exposes other request formats, but this implementation retains the existing labels/inputs/instructions contract. A 51-requirement run consumes 408 classifications before retries. Resolved models are retained; a service fallback is never relabeled as Jev.

Standalone mode serializes only the submitted requirement text and the context-mode marker. Document mode additionally serializes curated scope, terms, interfaces, constants, priorities, normative cross-references, local prose and boundary notes. The authored SC/F/M importance classifications, D/R verification assignments, acceptance examples, mutations and prior test outcomes are excluded. Source document, source code and question definitions are saved locally for reproducibility; local snapshots are not wholesale provider inputs.

Subject/trigger/completion use presence semantics: qualitative wording can name an actor, event or terminal outcome while remaining ambiguous, non-testable or quantitatively insufficient. Boolean or categorical obligations need not invent numeric thresholds. A conditional clause and one required result do not by themselves constitute a compound requirement. The sample sentence is not accompanied by an expected answer key.

`summary.completed` and `summary.abstained` count requirement rows, not individual questions. Per-axis counts are in `by_question`. Review candidates are an OR of the first seven unfavorable labels. If no unfavorable label is known and some questions abstain, the row remains indeterminate. Raw probabilities and provider confidence remain available; there is no fabricated overall correctness probability or automatic specification edit.
