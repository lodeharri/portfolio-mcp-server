# security-layers

## Purpose

The five-layer security model that protects every chunk that enters the index and every byte that leaves the server. Implementation lives in `src/mcp_server/security/`. All layers are mandatory; no tool may bypass redaction.

## Schema / Interface

```python
# src/mcp_server/security/manifest_loader.py
from typing import Protocol

class ManifestLoader(Protocol):
    def load(self, path: str) -> Manifest: ...
    def is_path_indexed(self, path: str) -> bool: ...   # default-deny

class Manifest(BaseModel):
    schema_version: int
    server: ManifestServer
    indexing: IndexingConfig
    projects: list[ProjectEntry]

class ProjectEntry(BaseModel):
    id: str
    path: str
    include_subdirs: list[str]
    exclude_subdirs: list[str]

# src/mcp_server/security/gitleaks_scanner.py
from enum import Enum

class ScanVerdict(Enum):
    BLOCKED  = "blocked"    # high confidence → refuse to insert
    FLAGGED  = "flagged"    # medium confidence → insert + audit
    CLEAN    = "clean"      # no findings

class GitleaksScanner(Protocol):
    def scan(self, content: str, source: str) -> ScanVerdict: ...

# src/mcp_server/security/output_sanitizer.py
from enum import Enum

class SecretPattern(Enum):
    AWS     = r"AKIA[0-9A-Z]{16}"
    GITHUB  = r"ghp_[a-zA-Z0-9]{36}"
    OPENAI  = r"sk-[a-zA-Z0-9]{48}"
    GEMINI  = r"AIza[0-9A-Za-z_-]{35}"
    GENERIC = r"(api[_-]?key|secret|password|token)\s*[:=]\s*[\S]+"

class RedactionResult(BaseModel):
    text: str                       # redacted body
    incidents: list[RedactionIncident]

class RedactionIncident(BaseModel):
    pattern: SecretPattern
    start: int
    end: int
    source: str                     # tool name, route, etc.

# src/mcp_server/security/rate_limiter.py
class RateLimiter(Protocol):
    def check(self, key: str) -> bool: ...   # True = allowed

# src/mcp_server/security/audit.py
class AuditLogger(Protocol):
    def info(self, event: str, **fields: Any) -> None: ...
    def warn(self, event: str, **fields: Any) -> None: ...
```

## Requirements

### Requirement: Manifest Loader is Default-Deny

The manifest loader MUST be the only entry point that decides which filesystem paths leave the index. It MUST refuse to enumerate any path not declared in `projects[].path` (plus the explicit `include_subdirs`/`exclude_subdirs`).

#### Scenario: Path inside declared project is indexable

- GIVEN `manifest.projects` lists `finance-coach-latam` at `/home/.../finance-coach-latam`
- WHEN `is_path_indexed("/home/.../finance-coach-latam/backend/api/users.py")` is called
- THEN it MUST return `True`.

#### Scenario: Path outside declared project is rejected

- GIVEN the manifest declares only the two portfolio siblings
- WHEN `is_path_indexed("/home/.../finance-coach-latam/.aws/credentials")` is called
- THEN it MUST return `False` (excluded subdir).
- AND `is_path_indexed("/home/.../some-other-project/src/main.py")` MUST return `False` (not declared).

#### Scenario: Invalid manifest schema is rejected

- GIVEN a manifest missing `schema_version` or `projects`
- WHEN `load()` is called
- THEN it MUST raise `ManifestSchemaError`
- AND the preindex pipeline MUST abort with a non-zero exit code.

### Requirement: Gitleaks Scanner — Block / Flag / Clean

The gitleaks wrapper MUST invoke `gitleaks detect --no-git --source <tmp>` on each chunk and translate gitleaks' exit code into `ScanVerdict`. High-confidence findings (`exit 1`) MUST be `BLOCKED`; medium-confidence findings (`exit 2` in future gitleaks versions) MUST be `FLAGGED`; no findings (`exit 0`) MUST be `CLEAN`.

#### Scenario: Block on high-confidence secret

- GIVEN a chunk containing `AKIAIOSFODNN7EXAMPLE`
- WHEN `scan(chunk, source="finance-coach-latam/backend/auth.py")` is called
- THEN the verdict MUST be `BLOCKED`
- AND the chunk MUST be excluded from the index
- AND an audit log entry MUST be emitted with `event="secret.blocked"` and the source path.

#### Scenario: Flag on medium-confidence secret

- GIVEN a chunk flagged as medium confidence by gitleaks
- WHEN `scan(...)` is called
- THEN the verdict MUST be `FLAGGED`
- AND the chunk MUST still be inserted (with the flag recorded).

#### Scenario: Clean content passes through

- GIVEN a chunk with no detectable secrets
- WHEN `scan(...)` is called
- THEN the verdict MUST be `CLEAN`.

#### Scenario: gitleaks binary missing fails closed

- GIVEN the `gitleaks` binary is not on `$PATH`
- WHEN `scan(...)` is called
- THEN the scanner MUST raise `GitleaksBinaryMissingError`
- AND the preindex pipeline MUST abort (fail-closed).

### Requirement: Output Sanitizer Redacts Known Patterns

The output sanitizer MUST replace every match of the five `SecretPattern` regexes with the literal string `[REDACTED]`. It MUST return both the redacted text and the list of incidents so the audit log can record what would have leaked.

#### Scenario: AWS access key is redacted

- GIVEN text containing `AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE`
- WHEN `sanitize(text)` is called
- THEN the result MUST contain `AWS_ACCESS_KEY_ID=[REDACTED]`
- AND a `RedactionIncident` with `pattern=AWS` MUST appear in `incidents`.

#### Scenario: GitHub personal access token is redacted

- GIVEN text containing `ghp_` followed by 36 word chars
- WHEN `sanitize(text)` is called
- THEN the token substring MUST be replaced
- AND an incident MUST be logged.

#### Scenario: Generic key=value secret is redacted

- GIVEN text containing `api_key=abc123` or `secret: hunter2`
- WHEN `sanitize(text)` is called
- THEN the value MUST be replaced with `[REDACTED]`.

#### Scenario: Clean text passes through unchanged

- GIVEN text with no matching patterns
- WHEN `sanitize(text)` is called
- THEN the returned text MUST equal the input
- AND `incidents` MUST be empty.

#### Scenario: OpenAI and Gemini keys are redacted (table-driven)

- GIVEN a list of `(pattern, sample)` pairs (`sk-` + 48 chars, `AIza` + 35 chars)
- WHEN `sanitize(sample)` is called for each
- THEN each MUST be redacted to `[REDACTED]`
- AND the corresponding `RedactionIncident.pattern` MUST match.

### Requirement: Rate Limiter Caps at 30 req/min/IP

The slowapi-backed rate limiter MUST reject the 31st request from a given IP within a rolling 60-second window with HTTP 429.

#### Scenario: First 30 requests in a minute succeed

- GIVEN a fresh client IP
- WHEN 30 sequential requests are sent within 60 s
- THEN every response MUST be 2xx (or whatever the route returns when not rate-limited).

#### Scenario: 31st request is rejected

- GIVEN the same client IP has already made 30 requests in the last 60 s
- WHEN it sends a 31st request
- THEN the response MUST be HTTP 429
- AND the body SHALL indicate the rate limit was hit.

### Requirement: Audit Logger Emits Structured JSON

The audit module MUST emit structlog JSON to stdout with at minimum the fields `event`, `timestamp`, `level`, plus free-form `**fields` per call. Every redaction, every blocked scan, and every rate-limit hit MUST be logged.

#### Scenario: Audit event is valid JSON

- GIVEN the audit logger is configured
- WHEN `audit.warn("secret.blocked", source="finance-coach-latam/backend/auth.py", pattern="AWS")` is called
- THEN stdout MUST contain a single JSON line
- AND the line MUST contain `event=="secret.blocked"`, `source`, `pattern`, and an ISO-8601 `timestamp`.

## Error / Edge Cases

- Concurrent redaction: sanitizer MUST be thread-safe (no shared mutable state).
- Manifest file missing: `load()` MUST raise `ManifestNotFoundError`; preindex MUST abort.
- Manifest file unreadable (permission denied): `load()` MUST raise `ManifestPermissionError`.
- Gitleaks returns malformed JSON / non-zero exit not in the known set: scanner MUST treat as `BLOCKED` (fail-closed).
- Rate limiter backend (slowapi default = in-memory) MUST NOT be assumed to share state across workers; the `--workers 1` choice in `app-bootstrap` makes this safe.

## Test Scenarios

| Scenario | Required because |
|---|---|
| `is_path_indexed` returns true for declared paths, false for others | **Layer 1** scoped indexing |
| Sanitizer redacts AWS / GitHub / OpenAI / Gemini / generic patterns | **Layer 3** output sanitization |
| Sanitizer is invoked on every `/healthz` response (covered in `app-bootstrap`) | **Layer 3** at exit boundary |
| Preindex chunked scan blocks `BLOCKED` chunks | **Layer 2** preindex secret scan |
| Slowapi rejects 31st request | **Layer 5** rate limit |
| Every redaction emits a structlog JSON line | **Layer 5** audit log |
