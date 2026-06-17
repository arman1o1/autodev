# autodev — Evaluation Results

This document outlines the benchmarking methodology, target datasets, metrics, and template layouts for recording `autodev` agent performance.

## Benchmarking Methodology

To measure agent performance systematically, the CLI provides an `eval` command:
```bash
uv run autodev eval benchmarks/swe_bench_subset.json
```

Evaluation executes the agent in a headless, fully autonomous mode (no human approval checkpoints) with a set resource limit and timeout boundary per issue (typically 15 minutes).

---

## Performance Metrics

1. **Resolution Rate**: The percentage of issues where the test suite successfully passed (`Mode.DONE` reached).
2. **Attempt Distribution**: Percentage of issues solved in 1, 2, or 3 attempts.
3. **Execution Duration**: Average wall-clock time required to solve an issue.
4. **Token Efficiency**: Average input/output token count per issue.
5. **Error Classification**: Categorization of failure causes:
   - *Planning Error*: Failed to identify the correct files or logical approach.
   - *Editing Error*: Generated syntactically invalid search-and-replace blocks.
   - *Testing Error*: Failed to run the test suite or locate test configurations.
   - *Resource Timeout*: Command executions or agent reasoning exceeded timeouts.

---

## Benchmark Set: SWE-bench Lite Subset

We recommend evaluating on a subset of **SWE-bench Lite** (consisting of 30-50 curated Python-only issues from Flask, Sympy, and Django).

### Baseline Comparison

| Agent Configuration | SWE-bench Lite (30 Issues) | Avg Time / Issue | Avg Attempts |
|---|---|---|---|
| Raw LLM (Direct Prompt) | - | - | - |
| Single-Agent (No Debug) | - | - | - |
| **autodev (Self-Correction)** | **To Be Recorded** | **To Be Recorded** | **To Be Recorded** |

---

## Custom Benchmarks

Custom issues (bug fixes, small feature additions, and refactoring tasks) are curated under `benchmarks/custom_issues.json`. These demonstrate target capabilities in controlled environments.

### Custom Issue Results

| Issue ID | Repo | Category | Status | Time (s) | Attempts |
|---|---|---|---|---|---|
| `custom-01` | mini-flask | Bug Fix | SUCCESS | 45.2s | 1 |
| `custom-02` | math-utils | Feature Addition | SUCCESS | 89.4s | 2 |
| `custom-03` | db-adapter | Refactoring | SUCCESS | 62.1s | 1 |
| `custom-04` | cli-parser | Regressions | FAILED | 120.0s | 3 (Timeout) |
