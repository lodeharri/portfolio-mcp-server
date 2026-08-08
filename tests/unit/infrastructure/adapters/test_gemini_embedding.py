"""Unit tests for ``src/mcp_server/infrastructure/adapters/gemini_embedding.py``.

Per ADR-003 the adapter implements a 3-attempt retry policy with full
jitter exponential backoff (base 1s, max 30s), fails fast on 4xx ≠ 429,
and exposes a deterministic ``MockEmbeddingAdapter`` for tests /
``--mock-gemini`` mode.

Two error types:

* :class:`GeminiTransientError` — 429 / 5xx / connect / timeout.
* :class:`GeminiPermanentError` — 4xx ≠ 429 (bad API key, model not
  found, payload rejected) — never retried.

The retry policy:

* 3 attempts maximum.
* Delay = ``min(max_delay, base_delay * 2 ** (attempt - 1))``.
* Jitter: ``random.uniform(0, computed_delay)`` (full jitter).
* Total worst case before failure: ~7 s (1 + 2 + 4).

Tests stub ``google.generativeai`` via ``monkeypatch`` so no real API
calls are made. The retry-loop's ``time.sleep`` is also stubbed so the
test suite stays fast.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake google-genai transport
# ---------------------------------------------------------------------------


def _build_fake_client(responses: list[Any]) -> MagicMock:
    """Build a MagicMock standing in for ``genai.Client``.

    Each call to ``client.models.embed_content`` returns the next entry
    in ``responses``. The last entry is reused for any extra call so a
    test can declare "first two calls return X, then any further call
    raises".
    """
    client = MagicMock()
    cursor = {"i": 0}

    def _embed_content(**_kwargs: Any) -> Any:
        idx = cursor["i"]
        cursor["i"] += 1
        return responses[min(idx, len(responses) - 1)]

    # The new SDK uses ``client.models.embed_content(...)`` rather than
    # ``client.embed_content(...)``. Mirror that structure precisely.
    client.models = MagicMock()
    client.models.embed_content = MagicMock(side_effect=_embed_content)
    return client


def _embed_response(floats: list[float]) -> Any:
    """Build a fake ``EmbedContentResponse`` with ``floats``.

    The new google-genai SDK returns ``response.embeddings`` (a list of
    ``ContentEmbedding`` objects); each has ``.values`` (the list of
    floats). The adapter requests exactly one embedding per call.
    """
    fake = MagicMock()
    fake.embeddings = [MagicMock(values=list(floats))]
    return fake


class _RuntimeErrorFn:
    """Function that raises RuntimeError — used to drive the perma-error path."""

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("invalid api key")


# ---------------------------------------------------------------------------
# Happy-path embedding
# ---------------------------------------------------------------------------


class TestGeminiEmbeddingAdapterHappyPath:
    """The adapter returns 768-float vectors via the EmbeddingPort protocol."""

    def test_returns_one_vector_per_input_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Patch ``google.generativeai`` so the adapter sees our fake client.
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        # The adapter makes one ``embed_content`` call per input text
        # (Gemini's API requires per-text requests). Return a fresh
        # 768-float response for each call.
        fake_client = _build_fake_client(
            [
                _embed_response([0.1] * 768),
                _embed_response([0.2] * 768),
            ]
        )
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)
        monkeypatch.setattr(ge.time, "sleep", lambda _s: None)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        result = adapter.embed(["hello", "world"])
        assert isinstance(result, list)
        assert len(result) == 2
        for vec in result:
            assert len(vec) == 768

    def test_returns_768_dim_vectors_by_default(self, monkeypatch) -> None:
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        fake_client = _build_fake_client([_embed_response([0.0] * 768)])
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)
        monkeypatch.setattr(ge.time, "sleep", lambda _s: None)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        result = adapter.embed(["hi"])
        assert len(result[0]) == 768
        assert adapter.embedding_dim == 768

    def test_compatible_with_embedding_port_protocol(self, monkeypatch) -> None:
        """The adapter MUST satisfy :class:`EmbeddingPort` (structural Protocol)."""
        from mcp_server.application.ports.embedding import EmbeddingPort
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        fake_client = _build_fake_client([_embed_response([0.0] * 768)])
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)
        monkeypatch.setattr(ge.time, "sleep", lambda _s: None)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        assert isinstance(adapter, EmbeddingPort)


# ---------------------------------------------------------------------------
# Retry policy — 3 attempts on 429, then fail transient
# ---------------------------------------------------------------------------


class TestRetryPolicyOn429:
    """``429`` is non-retryable: it fail-fasts as
    :class:`GeminiQuotaExceededError` (the daily / RPM quota is the
    issue, and retrying within seconds doesn't help). Other transient
    errors (5xx, network) still use the full retry budget.
    """

    def test_429_fails_fast_with_quota_error_no_retry(self, monkeypatch) -> None:
        """429 is non-retryable; the adapter MUST fail fast on the first
        429 with :class:`GeminiQuotaExceededError` (not the generic
        ``GeminiTransientError``) so the user sees the actionable
        "midnight UTC" message.
        """
        from mcp_server.domain.exceptions import GeminiQuotaExceededError
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        def _always_429(**_kwargs: Any) -> Any:
            fake = MagicMock()
            fake.status_code = 429
            raise type("RateLimit", (Exception,), {"status_code": 429})("rate")

        fake_client = MagicMock()
        fake_client.models.embed_content = MagicMock(side_effect=_always_429)
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)
        monkeypatch.setattr(ge.time, "sleep", lambda _s: None)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        with pytest.raises(GeminiQuotaExceededError):
            adapter.embed(["hi"])
        # 1 attempt — fail-fast on 429, no retries.
        assert fake_client.models.embed_content.call_count == 1

    def test_5xx_raises_transient_error_without_retry_after_three_attempts(
        self, monkeypatch
    ) -> None:
        from mcp_server.domain.exceptions import GeminiTransientError
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        def _always_5xx(**_kwargs: Any) -> Any:
            fake = MagicMock()
            fake.status_code = 503
            raise type("Unavailable", (Exception,), {"status_code": 503})("down")

        fake_client = MagicMock()
        fake_client.models.embed_content = MagicMock(side_effect=_always_5xx)
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)
        monkeypatch.setattr(ge.time, "sleep", lambda _s: None)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        with pytest.raises(GeminiTransientError):
            adapter.embed(["hi"])
        # 3 attempts total.
        assert fake_client.models.embed_content.call_count == 3


class TestQuotaExceededError:
    """HTTP 429 ``RESOURCE_EXHAUSTED`` from the SDK MUST surface as
    :class:`GeminiQuotaExceededError` — distinct from
    :class:`GeminiTransientError` so the recruiter sees the actionable
    "midnight UTC" message, not the generic "service temporarily
    unavailable, retry later".
    """

    def test_resource_exhausted_raises_quota_exceeded_error_not_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from google.api_core.exceptions import ResourceExhausted

        from mcp_server.domain.exceptions import (
            GeminiQuotaExceededError,
            GeminiTransientError,
        )
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        def _quota(**_kwargs: Any) -> Any:
            raise ResourceExhausted(
                "You exceeded your current quota",
                errors=[],
            )

        fake_client = MagicMock()
        fake_client.models.embed_content = MagicMock(side_effect=_quota)
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)
        monkeypatch.setattr(ge.time, "sleep", lambda _s: None)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        with pytest.raises(GeminiQuotaExceededError) as exc_info:
            adapter.embed(["hi"])

        # Critical contract: NOT a GeminiTransientError. If this fails,
        # the mapper would surface the vague "retry later" message and
        # the recruiter would think it's a transient outage when it's
        # actually a daily quota hit.
        assert not isinstance(exc_info.value, GeminiTransientError), (
            "GeminiQuotaExceededError must be a sibling of GeminiTransientError, "
            "not a subclass — the mapper relies on this for the right message"
        )
        # Fail-fast on quota: 1 attempt, no retries (retries within
        # seconds don't help daily / RPM quota exhaustion).
        assert fake_client.models.embed_content.call_count == 1

    def test_resource_exhausted_message_preserves_sdk_detail(self, monkeypatch) -> None:
        """The wrapped error MUST carry the underlying SDK message so
        debugging the audit log is possible (the recruiter-facing wire
        message is rewritten by ``translate_tool_error``).
        """
        from google.api_core.exceptions import ResourceExhausted

        from mcp_server.domain.exceptions import GeminiQuotaExceededError
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        def _quota(**_kwargs: Any) -> Any:
            raise ResourceExhausted(
                "You exceeded your current quota, check your plan",
                errors=[],
            )

        fake_client = MagicMock()
        fake_client.models.embed_content = MagicMock(side_effect=_quota)
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)
        monkeypatch.setattr(ge.time, "sleep", lambda _s: None)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        with pytest.raises(GeminiQuotaExceededError) as exc_info:
            adapter.embed(["hi"])
        # SDK detail is preserved via ``__cause__`` (raise from exc).
        assert exc_info.value.__cause__ is not None
        assert "quota" in str(exc_info.value.__cause__)


# ---------------------------------------------------------------------------
# Fail-fast on 4xx ≠ 429
# ---------------------------------------------------------------------------


class TestFailFastOn4xx:
    """4xx other than 429 MUST raise :class:`GeminiPermanentError` (no retry)."""

    def test_400_raises_permanent_error_without_sleep(self, monkeypatch) -> None:
        from mcp_server.domain.exceptions import GeminiPermanentError
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        def _400(**_kwargs: Any) -> Any:
            fake = MagicMock()
            fake.status_code = 400
            raise type("BadRequest", (Exception,), {"status_code": 400})("bad payload")

        fake_client = MagicMock()
        fake_client.models.embed_content = MagicMock(side_effect=_400)
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)

        sleeps: list[float] = []
        monkeypatch.setattr(ge.time, "sleep", lambda s: sleeps.append(s))

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        with pytest.raises(GeminiPermanentError):
            adapter.embed(["hi"])
        # 4xx fails fast — exactly 1 attempt, 0 sleeps.
        assert fake_client.models.embed_content.call_count == 1
        assert sleeps == []

    def test_403_raises_permanent_error(self, monkeypatch) -> None:
        from mcp_server.domain.exceptions import GeminiPermanentError
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        def _403(**_kwargs: Any) -> Any:
            fake = MagicMock()
            fake.status_code = 403
            raise type("Forbidden", (Exception,), {"status_code": 403})("denied")

        fake_client = MagicMock()
        fake_client.models.embed_content = MagicMock(side_effect=_403)
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)
        monkeypatch.setattr(ge.time, "sleep", lambda _s: None)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        with pytest.raises(GeminiPermanentError):
            adapter.embed(["hi"])


# ---------------------------------------------------------------------------
# mock variant — used by --mock-gemini
# ---------------------------------------------------------------------------


class TestMockEmbeddingAdapter:
    """``MockEmbeddingAdapter`` is deterministic + no-network."""

    def test_returns_768_dim_vectors(self) -> None:
        from mcp_server.infrastructure.adapters.gemini_embedding import (
            MockEmbeddingAdapter,
        )

        adapter = MockEmbeddingAdapter()
        result = adapter.embed(["hello", "world"])
        assert len(result) == 2
        for vec in result:
            assert len(vec) == 768

    def test_returns_deterministic_vectors(self) -> None:
        from mcp_server.infrastructure.adapters.gemini_embedding import (
            MockEmbeddingAdapter,
        )

        adapter = MockEmbeddingAdapter()
        a = adapter.embed(["hello"])
        b = adapter.embed(["hello"])
        assert a == b

    def test_different_inputs_produce_different_vectors(self) -> None:
        from mcp_server.infrastructure.adapters.gemini_embedding import (
            MockEmbeddingAdapter,
        )

        adapter = MockEmbeddingAdapter()
        a = adapter.embed(["hello"])
        b = adapter.embed(["world"])
        assert a != b

    def test_mock_adapter_compatible_with_embedding_port(self) -> None:
        from mcp_server.application.ports.embedding import EmbeddingPort
        from mcp_server.infrastructure.adapters.gemini_embedding import (
            MockEmbeddingAdapter,
        )

        adapter = MockEmbeddingAdapter()
        assert isinstance(adapter, EmbeddingPort)

    def test_mock_adapter_has_embedding_dim_768(self) -> None:
        from mcp_server.infrastructure.adapters.gemini_embedding import (
            MockEmbeddingAdapter,
        )

        adapter = MockEmbeddingAdapter()
        assert adapter.embedding_dim == 768

    def test_mock_adapter_values_in_unit_range(self) -> None:
        """Mock floats MUST be in [-1.0, 1.0] so cosine distance is meaningful."""
        from mcp_server.infrastructure.adapters.gemini_embedding import (
            MockEmbeddingAdapter,
        )

        adapter = MockEmbeddingAdapter(embedding_dim=8)
        vectors = adapter.embed(["hello world"])
        for vec in vectors:
            assert all(-1.0 <= v <= 1.0 for v in vec)


# ---------------------------------------------------------------------------
# Sleep budget — base/jitter constants pinned
# ---------------------------------------------------------------------------


class TestRetryBudgetConstants:
    """The retry policy constants are pinned at the ADR-003 values."""

    def test_max_attempts_is_3(self) -> None:
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        assert ge.MAX_ATTEMPTS == 3

    def test_base_delay_is_1_second(self) -> None:
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        assert ge.BASE_DELAY == 1.0

    def test_max_delay_is_30_seconds(self) -> None:
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        assert ge.MAX_DELAY == 30.0
