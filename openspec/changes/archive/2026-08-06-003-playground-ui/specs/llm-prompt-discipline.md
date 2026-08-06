# llm-prompt-discipline — Delta Specification

## Purpose

The cross-cutting prompt-engineering principle (Decision #12) applied to
every LLM-facing call in the playground. The principle has three legs:

1. **Scoped** — pass only the minimum context the agent needs to answer
   the recruiter's question. No "you are an expert assistant with access
   to many tools" boilerplate unless it is load-bearing for correctness.
2. **Short-first** — default `max_tokens` is the minimum that completes
   the typical answer, not the maximum the model can generate. The
   existing `explain_architecture=500` and `summarize_readme=300` are
   reduced by ~30 % where the typical answer fits.
3. **Complete on the critical path** — never sacrifice the answer's
   correctness or the user's understanding of it for terseness. The
   principle is "don't waste", not "truncate".

This is enforced at the spec level (each tool spec declares its
`max_tokens` default and system prompt content) and at the test level
(LLM call assertions check that the system prompt sent matches the
expected template and that the `max_tokens` parameter equals the
declared default).

## Concrete Defaults

The following `max_tokens` defaults are proposed for confirmation in the
design phase:

| Tool | Previous default | New default | Rationale |
|---|---|---|---|
| `explain_architecture` | 500 | **350** | Recruiter summary fits comfortably in 300–350 tokens; ADR is the source of truth, not the summary |
| `summarize_readme` | 300 | **200** | Recruiter punchy one-liner; README is summarized, not paraphrased |
| `ask_portfolio` | (no explicit default; bounded by Pydantic AI usage limits) | **600** | Aggregates 5 sibling tools; needs headroom for synthesis but not essay-length |

> **Open for design-phase confirmation:** the user has the final call on
> the new defaults. The proposal (Decision #12) directs "reduce by ~30 %
> if the typical answer fits"; the values above are the spec author's
> interpretation of "fits". The design phase should either confirm or
> request adjustments before implementation.

## Schema / Interface

```python
# src/mcp_server/application/use_cases/explain_architecture.py — MODIFIED
@dataclass(frozen=True)
class ExplainArchitectureRequest:
    project_id: str
    max_tokens: int = 350            # was 500

# src/mcp_server/application/use_cases/summarize_readme.py — MODIFIED
@dataclass(frozen=True)
class SummarizeReadmeRequest:
    project_id: str
    max_tokens: int = 200            # was 300

# src/mcp_server/application/use_cases/ask_portfolio.py — MODIFIED
# Agent's per-call usage cap (passed via Pydantic AI UsageLimits) keeps
# the assistant reply under 600 tokens by default. The MCP tool's
# ask_portfolio_tool signature keeps max_tokens optional and forwards
# to the use case.
```

## ADDED Requirements

### Requirement: System Prompts Are Scoped (No Boilerplate)

Every system prompt sent by a use case in the playground MUST contain
ONLY the minimum context required for the tool to do its job. The
prompt MUST NOT contain generic "you are an expert assistant"
boilerplate, MUST NOT list capabilities the tool does not have, and
MUST NOT include motivational phrasing ("be concise, be helpful")
unless that phrasing directly affects correctness (e.g., "answer in one
paragraph" when the response is meant to be a one-paragraph summary).

#### Scenario: explain_architecture system prompt is scoped

- GIVEN `ExplainArchitectureUseCase.execute` is invoked
- WHEN the prompt sent to `LLMPort.summarize` is captured
- THEN it MUST include the ADR file content
- AND it MUST specify the expected output shape
  (project architecture summary, ≤ one paragraph, mention tradeoffs)
- AND it MUST NOT include "you are an expert architect" or similar
  boilerplate that does not change the answer.

#### Scenario: summarize_readme system prompt is scoped

- GIVEN `SummarizeReadmeUseCase.execute` is invoked
- WHEN the prompt sent to `LLMPort.summarize` is captured
- THEN it MUST include the README content
- AND it MUST specify the expected output shape
  (one-paragraph recruiter-friendly summary)
- AND it MUST NOT exceed ~150 words of instructions on top of the
  README content.

#### Scenario: ask_portfolio agent system prompt is scoped

- GIVEN the Pydantic AI agent is built in `composition.compose()`
- WHEN the agent's system prompt is captured
- THEN it MUST specify the recruiter-demo context
- AND it MUST list the 5 sibling tools the agent can call
- AND it MUST prefer short answers
- AND it MUST NOT contain generic "you are a helpful AI" boilerplate
  unless that phrasing is load-bearing for correctness.

### Requirement: max_tokens Defaults Are Reduced (Short-First)

The default `max_tokens` for each tool MUST be reduced from the current
value by ~30 % where the typical answer fits, and MUST NOT be reduced
below what correctness requires. The new defaults proposed are:
`explain_architecture=350`, `summarize_readme=200`, `ask_portfolio=600`.

#### Scenario: explain_architecture defaults to 350 tokens

- GIVEN a fresh request without an explicit `max_tokens` argument
- WHEN `ExplainArchitectureUseCase.execute` is invoked
- THEN the `LLMPort.summarize` call MUST be made with
  `max_tokens=350`
- AND a unit test MUST assert this default.

#### Scenario: summarize_readme defaults to 200 tokens

- GIVEN a fresh request without an explicit `max_tokens` argument
- WHEN `SummarizeReadmeUseCase.execute` is invoked
- THEN the `LLMPort.summarize` call MUST be made with
  `max_tokens=200`
- AND a unit test MUST assert this default.

#### Scenario: ask_portfolio caps at 600 tokens

- GIVEN the agent is invoked with no explicit usage cap
- WHEN `AskPortfolioUseCase.aexecute` (or `astream`) is invoked
- THEN the Pydantic AI `UsageLimits` MUST include
  `response_tokens_limit=600` (or the equivalent in the pinned
  `pydantic-ai` version)
- AND a unit test MUST assert this default.

#### Scenario: Explicit override is honored

- GIVEN the caller passes `max_tokens=1000` to `explain_architecture`
- WHEN the use case runs
- THEN the LLM call MUST use the caller-supplied value (350 is only the
  default, not a hard cap).

### Requirement: System Prompts Are Pinned by Unit Tests

Every use case MUST have a unit test that asserts the system prompt
sent to the LLM matches the expected template (string equality or
regex match against a frozen fixture). The fixture MUST be checked into
the repo so changes to the prompt are a deliberate PR decision.

#### Scenario: explain_architecture prompt fixture exists

- GIVEN `tests/unit/application/use_cases/test_explain_architecture.py`
  exists
- WHEN the test suite runs
- THEN a test MUST assert the exact system prompt string sent to
  `LLMPort.summarize` matches `tests/fixtures/prompts/
  explain_architecture_system.txt`
- AND the fixture MUST be ≤ 200 words (Scoped invariant).

#### Scenario: summarize_readme prompt fixture exists

- GIVEN `tests/unit/application/use_cases/test_summarize_readme.py`
  exists
- WHEN the test suite runs
- THEN a test MUST assert the system prompt matches
  `tests/fixtures/prompts/summarize_readme_system.txt`.

#### Scenario: ask_portfolio agent prompt fixture exists

- GIVEN `tests/unit/composition/test_agent_prompt.py` exists
- WHEN the test suite runs
- THEN a test MUST assert the agent's `system_prompt` matches
  `tests/fixtures/prompts/ask_portfolio_agent_system.txt`.

### Requirement: max_tokens Defaults Are Pinned by Unit Tests

Every use case MUST have a unit test that asserts the default
`max_tokens` parameter sent to the LLM is the new (reduced) value, and
that an explicit override is respected. The test is the regression
guard so a future change that "restores" the old default will fail
loudly.

#### Scenario: Default max_tokens is the new (reduced) value

- GIVEN the unit test runs with a default request (no `max_tokens`
  argument)
- WHEN the LLM adapter is mocked and the call args are captured
- THEN the test MUST assert `max_tokens == <new default>` for each tool.

#### Scenario: Override max_tokens is respected

- GIVEN the caller passes `max_tokens=1000`
- WHEN the LLM adapter is mocked
- THEN the test MUST assert the captured `max_tokens == 1000` (not the
  default).

## Error / Edge Cases

- A future tool that needs MORE tokens than the typical answer (e.g.,
  a future "explain_full_architecture" deep-dive) MUST be a new tool
  with its own `max_tokens` default; the existing `explain_architecture`
  default MUST stay at 350.
- Pydantic AI `UsageLimits` field names may differ across releases
  (`response_tokens_limit` in 2.x, may be `max_tokens` or
  `output_tokens` in other versions); the unit test MUST be pinned to
  the field name documented in the `pydantic-ai` version range
  declared in `pyproject.toml`.

## Test Scenarios

| Scenario | Required because |
|---|---|
| `explain_architecture` prompt fixture matches the live call | Scoped invariant |
| `summarize_readme` prompt fixture matches the live call | Scoped invariant |
| `ask_portfolio` agent prompt fixture matches the live call | Scoped invariant |
| `explain_architecture` default `max_tokens == 350` | Short-first invariant |
| `summarize_readme` default `max_tokens == 200` | Short-first invariant |
| `ask_portfolio` default response cap == 600 | Short-first invariant |
| Caller-supplied `max_tokens` overrides the default | Override escape hatch |
