"""Gemini embedding adapter — implements :class:`EmbeddingPort`.

Per ADR-003 the retry policy is:

* 3 attempts maximum.
* Exponential backoff with full jitter:
  ``computed = min(max_delay, base_delay * 2 ** (attempt - 1))``,
  ``actual = random.uniform(0, computed)``.
* Retry on HTTP 429, 5xx, network errors, timeouts.
* Fail fast on HTTP 4xx other than 429.

The 0.1-second pacing between *successful* calls lives in the preindex
use case (``PreindexUseCase``), NOT here. The adapter only sleeps
between retry attempts; the use case paces inter-call budget per ADR-003
follow-up.

Two error types:

* :class:`GeminiTransientError` — retryable.
* :class:`GeminiPermanentError` — non-retryable (4xx ≠ 429).

Mock variant
------------

:class:`MockEmbeddingAdapter` implements the same port with deterministic
hash-of-text → 768-float vectors so tests and ``--mock-gemini`` runs are
fast and reproducible.

SDK migration note
-------------------

This module uses the new ``google-genai`` SDK (the official replacement
for the deprecated ``google-generativeai``). The new SDK is ~50 MB
smaller at install time because it no longer pulls in
``google-api-python-client`` (the deprecated cache-uploader used by the
old SDK). See ``verify-report-pr4.md`` for the size rationale.
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Final, Protocol

from google import genai
from google.genai import types

from mcp_server.application.ports.embedding import EmbeddingPort
from mcp_server.domain.exceptions import GeminiPermanentError, GeminiTransientError

__all__ = [
    "MockEmbeddingAdapter",
    "GeminiEmbeddingAdapter",
    "MAX_ATTEMPTS",
    "BASE_DELAY",
    "MAX_DELAY",
]

# ---------------------------------------------------------------------------
# ADR-003 retry budget — exported for tests
# ---------------------------------------------------------------------------

MAX_ATTEMPTS: Final[int] = 3
BASE_DELAY: Final[float] = 1.0
MAX_DELAY: Final[float] = 30.0
DEFAULT_EMBEDDING_DIM: Final[int] = 768
DEFAULT_EMBEDDING_MODEL: Final[str] = "text-embedding-004"  # 768-dim, free tier


# ---------------------------------------------------------------------------
# Pluggable client builder — tests monkeypatch this
# ---------------------------------------------------------------------------


def _build_genai_client(api_key: str) -> "genai.Client":
    """Build a real ``google.genai.Client`` for production use.

    The new SDK uses a stateless client created once with the API key;
    requests are bound to the model at call time. This lets the same
    client back both embedding and chat endpoints.

    Tests override this function via the ``client_factory`` constructor
    argument to inject a fake transport.
    """
    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# Helpers — exception classification
# ---------------------------------------------------------------------------


_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


def _status_from_exception(exc: BaseException) -> int | None:
    """Read a ``status_code`` attribute off the SDK exception if present.

    The new google-genai SDK raises ``google.api_core.exceptions.*`` that
    carry ``status_code``. Falling back to ``None`` lets the caller treat
    anything without a status as a network/parsing error → transient.
    """
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    return None


# ---------------------------------------------------------------------------
# GeminiEmbeddingAdapter — real adapter
# ---------------------------------------------------------------------------


class _ClientLike(Protocol):
    """Structural shape the adapter needs from the SDK client.

    Lets the test fake supply a ``MagicMock`` with the same interface.
    """

    def models(self) -> object: ...


class GeminiEmbeddingAdapter:
    """EmbeddingPort implementation backed by ``google-genai``.

    Args:
        api_key: Gemini API key. Used to configure the new SDK client.
        model: Gemini embedding model identifier. Default
            ``"text-embedding-004"`` (768-dim, free tier).
        embedding_dim: Expected output dimension. Default 768.
        clock: Pluggable sleep source — tests inject a no-op so the
            retry loop stays fast. Default ``time.sleep``.
        client_factory: Pluggable client builder — defaults to
            :func:`_build_genai_client`. Tests override this to inject
            a fake transport.

    Raises:
        GeminiPermanentError: on 4xx ≠ 429 (fail-fast, no retry).
        GeminiTransientError: on 429, 5xx, network errors after
            exhausting ``MAX_ATTEMPTS`` attempts.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
        client_factory=None,
        clock=None,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiEmbeddingAdapter requires a non-empty api_key")
        self._api_key = api_key
        self._model = model
        self.embedding_dim = embedding_dim
        self._client: _ClientLike = (client_factory or _build_genai_client)(api_key)
        self._sleep = clock if clock is not None else time.sleep

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts with retry + fail-fast policy.

        One ``client.models.embed_content(...)`` call per text — the SDK
        does not natively batch, so the adapter loops over ``texts``. The
        0.1 s pacing between *successful* calls is the use case's
        responsibility (per ADR-003 follow-up); this method only sleeps
        on retries.

        Returns:
            One ``list[float]`` per input. ``len(result[i]) == embedding_dim``.
        """
        results: list[list[float]] = []
        for text in texts:
            results.append(self._embed_single(text))
        return results

    def _embed_single(self, text: str) -> list[float]:
        """Embed one text with the ADR-003 retry policy."""
        last_exc: BaseException | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.models.embed_content(
                    model=self._model,
                    contents=text,
                    config=types.EmbedContentConfig(
                        task_type="RETRIEVAL_DOCUMENT",
                    ),
                )
                return self._extract_single(response)
            except GeminiPermanentError:
                raise  # propagates without retry
            except Exception as exc:  # noqa: BLE001 — broad catch by design
                status = _status_from_exception(exc)
                if status is not None and 400 <= status < 500 and status != 429:
                    # Fail fast on 4xx (except 429 which is retryable).
                    raise GeminiPermanentError(
                        f"gemini embedding failed with status {status}: {exc}"
                    ) from exc

                last_exc = exc
                if attempt < MAX_ATTEMPTS:
                    self._sleep_retry(attempt)

        # All attempts exhausted — surface as transient.
        msg = (
            f"gemini embedding exhausted {MAX_ATTEMPTS} retries: "
            f"{type(last_exc).__name__ if last_exc else 'unknown'}: {last_exc}"
        )
        raise GeminiTransientError(msg) from last_exc

    def _sleep_retry(self, attempt: int) -> None:
        """Sleep for a full-jitter backoff between retry attempts."""
        computed = min(MAX_DELAY, BASE_DELAY * (2 ** (attempt - 1)))
        delay = random.uniform(0, computed)
        self._sleep(delay)

    def _extract_single(self, response: object) -> list[float]:
        """Extract the single vector from a google-genai ``EmbedContentResponse``.

        The new SDK returns ``response.embeddings`` (a list of
        ``ContentEmbedding`` objects); each has ``.values`` (the list of
        floats). We always request one embedding per call so the list
        has exactly one element.
        """
        embeddings = getattr(response, "embeddings", None)
        if not isinstance(embeddings, list) or not embeddings:
            raise GeminiTransientError(
                "malformed gemini response: missing embeddings list"
            )

        first = embeddings[0]
        values = getattr(first, "values", None)
        if not isinstance(values, list):
            raise GeminiTransientError(
                "malformed gemini response: missing values in ContentEmbedding"
            )
        return [float(v) for v in values]


# ---------------------------------------------------------------------------
# MockEmbeddingAdapter — deterministic, no-network
# ---------------------------------------------------------------------------


class MockEmbeddingAdapter:
    """Deterministic EmbeddingPort impl backed by SHA-256 hash of the text.

    The 768-float vector is derived from the SHA-256 digest of the input
    text: each 4-byte chunk becomes a float in [-1, 1] via
    ``(chunk % 2**32) / 2**31 - 1``. Same input → same vector across
    processes. No SDK, no network.

    Used by:

    * ``--mock-gemini`` CLI flag.
    * Tests that need a real ``EmbeddingPort`` instance without standing
      up the SDK or hitting the rate-limited free tier.
    """

    def __init__(self, embedding_dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        self.embedding_dim = embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return 768-float deterministic vectors for each text.

        The hash uses ``blake2b`` because the SHA-256 digest of typical
        source-code chunks produces identical leading 4 bytes — ``blake2b``
        spreads entropy better across the whole digest.
        """
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        # blake2b digest_size caps at 64 bytes (the maximum block size
        # for that algorithm). For 768 floats we need 768 * 4 = 3072
        # bytes, so we hash repeatedly and concatenate until we have
        # enough material.
        needed_bytes = self.embedding_dim * 4
        chunks: list[bytes] = []
        counter = 0
        while sum(len(c) for c in chunks) < needed_bytes:
            h = hashlib.blake2b(
                text.encode("utf-8") + f":{counter}".encode("ascii"),
                digest_size=64,
            ).digest()
            chunks.append(h)
            counter += 1
        buf = b"".join(chunks)[:needed_bytes]
        # Convert each 4-byte chunk to a float in [-1, 1].
        ints = [int.from_bytes(buf[i : i + 4], "big", signed=False) for i in range(0, len(buf), 4)]
        denom = 2**32 - 1
        return [((v / denom) * 2.0) - 1.0 for v in ints[: self.embedding_dim]]
