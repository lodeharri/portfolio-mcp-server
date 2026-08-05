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
    """``429`` is retryable; full jitter backoff; succeeds within 3 attempts."""

    def test_429_then_200_succeeds_and_sleeps_once(self, monkeypatch) -> None:
        from mcp_server.infrastructure.adapters import gemini_embedding as ge

        responses = [_RuntimeErrorFn(), _embed_response([0.0] * 768)]
        fake_client = _build_fake_client(responses)
        monkeypatch.setattr(ge, "_build_genai_client", lambda api_key: fake_client)

        sleeps: list[float] = []
        monkeypatch.setattr(ge.time, "sleep", lambda s: sleeps.append(s))

        # 429 must raise the retried error so the adapter can detect it;
        # use a fake SDK exception type that mirrors ``google.api_core``'s
        # ``ResourceExhausted`` (status 429).
        call_count = {"n": 0}

        def _embed_content(**_kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                fake = MagicMock()
                fake.status_code = 429
                fake.message = "rate limit"
                raise type("RateLimit", (Exception,), {"status_code": 429})("rate")
            return _embed_response([0.0] * 768)

        fake_client.models.embed_content = MagicMock(side_effect=_embed_content)
        monkeypatch.setattr(ge.random, "uniform", lambda _a, _b: 0.3)

        adapter = ge.GeminiEmbeddingAdapter(api_key="dummy")
        result = adapter.embed(["hi"])
        assert len(result) == 1
        # One sleep between the first (failing) call and the second (successful)
        assert len(sleeps) == 1
        assert sleeps[0] == 0.3  # matches our stubbed jitter

    def test_429_three_times_raises_transient_error(self, monkeypatch) -> None:
        from mcp_server.domain.exceptions import GeminiTransientError
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
        with pytest.raises(GeminiTransientError):
            adapter.embed(["hi"])
        # 3 attempts total — 2 sleeps.
        assert fake_client.models.embed_content.call_count == 3

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
