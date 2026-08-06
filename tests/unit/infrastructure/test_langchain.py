from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_server.application.ports.agent import AgentRequest
from mcp_server.infrastructure.langchain import (
    LangChainAgentAdapter,
    LangChainChunkingAdapter,
    LangChainEmbeddingAdapter,
    _MockLangChainEmbeddingAdapter,
    create_langchain_embedding,
)


def test_python_chunking_preserves_offsets() -> None:
    content = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
    adapter = LangChainChunkingAdapter(chunk_size=35, chunk_overlap=5)

    chunks = adapter.chunk(content, Path("example.py"))

    assert len(chunks) >= 2
    assert all(content[chunk.start_char : chunk.end_char] == chunk.text for chunk in chunks)


def test_markdown_chunking_uses_heading_boundaries() -> None:
    content = "# First\n\nAlpha paragraph.\n\n# Second\n\nBeta paragraph."
    adapter = LangChainChunkingAdapter(chunk_size=30, chunk_overlap=0)

    chunks = adapter.chunk(content, Path("README.md"))

    assert [chunk.text for chunk in chunks] == [
        "# First\n\nAlpha paragraph.",
        "# Second\n\nBeta paragraph.",
    ]


def test_empty_content_returns_no_chunks() -> None:
    adapter = LangChainChunkingAdapter()

    assert adapter.chunk("", Path("empty.txt")) == []


@pytest.mark.asyncio
async def test_agent_unwraps_tools_and_extracts_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
            captured["payload"] = payload
            captured["config"] = config
            return {
                "messages": [
                    SimpleNamespace(content="", tool_calls=[{"name": "search_code"}]),
                    SimpleNamespace(content="portfolio answer", tool_calls=[]),
                ]
            }

    def fake_create_react_agent(llm: Any, tools: list[Any]) -> FakeAgent:
        captured["tools"] = tools
        return FakeAgent()

    monkeypatch.setattr(
        "mcp_server.infrastructure.langchain.create_react_agent",
        fake_create_react_agent,
    )

    def tool_function() -> None:
        return None

    adapter = LangChainAgentAdapter(api_key="test", llm=object())

    response = await adapter.run(
        AgentRequest(question="Which project?", max_tool_calls=3),
        [SimpleNamespace(fn=tool_function)],
    )

    assert captured["tools"] == [tool_function]
    assert captured["config"] == {"recursion_limit": 7}
    assert response.answer == "portfolio answer"
    assert response.tool_calls == [{"name": "search_code"}]


# ---------------------------------------------------------------------------
# LangChainEmbeddingAdapter — delegates to LangChain's embed_documents
# ---------------------------------------------------------------------------


class TestLangChainEmbeddingAdapter:
    """The real adapter delegates to LangChain's ``embed_documents``.

    The LangChain client is mocked so no network calls happen — this is
    a unit test, not an integration test.
    """

    def test_delegates_to_langchain_embed_documents(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class FakeLangChainEmbeddings:
            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                captured["texts"] = list(texts)
                # One 4-float vector per input text — tiny for the test.
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        def fake_google_embeddings(*, model: str, google_api_key: str, **kwargs: Any) -> Any:
            captured["model"] = model
            captured["api_key"] = google_api_key
            return FakeLangChainEmbeddings()

        monkeypatch.setattr(
            "mcp_server.infrastructure.langchain.GoogleGenerativeAIEmbeddings",
            fake_google_embeddings,
        )

        adapter = LangChainEmbeddingAdapter(api_key="dummy-key", embedding_dim=4)

        result = adapter.embed(["alpha", "beta"])

        assert captured["model"] == "gemini-embedding-001"
        assert captured["api_key"] == "dummy-key"
        assert captured["texts"] == ["alpha", "beta"]
        assert result == [[0.1, 0.2, 0.3, 0.4], [0.1, 0.2, 0.3, 0.4]]

    def test_exposes_embedding_dim_attribute(self) -> None:
        adapter = LangChainEmbeddingAdapter(api_key="dummy", embedding_dim=768)
        assert adapter.embedding_dim == 768

    def test_satisfies_embedding_port_protocol(self) -> None:
        from mcp_server.application.ports.embedding import EmbeddingPort

        adapter = LangChainEmbeddingAdapter(api_key="dummy")
        assert isinstance(adapter, EmbeddingPort)


# ---------------------------------------------------------------------------
# _MockLangChainEmbeddingAdapter — deterministic SHA-256 mock
# ---------------------------------------------------------------------------


class TestMockLangChainEmbeddingAdapter:
    """The mock variant is deterministic, hash-backed, and protocol-compatible."""

    def test_returns_one_vector_per_input_text(self) -> None:
        adapter = _MockLangChainEmbeddingAdapter(embedding_dim=16)
        result = adapter.embed(["hello", "world"])
        assert len(result) == 2
        assert all(len(vec) == 16 for vec in result)

    def test_vectors_are_deterministic(self) -> None:
        adapter = _MockLangChainEmbeddingAdapter(embedding_dim=32)
        a = adapter.embed(["same input"])
        b = adapter.embed(["same input"])
        assert a == b

    def test_different_inputs_produce_different_vectors(self) -> None:
        adapter = _MockLangChainEmbeddingAdapter(embedding_dim=32)
        a = adapter.embed(["alpha"])
        b = adapter.embed(["beta"])
        assert a != b

    def test_values_are_in_unit_range(self) -> None:
        """Floats are normalized to [0, 1] so cosine distance is meaningful."""
        adapter = _MockLangChainEmbeddingAdapter(embedding_dim=64)
        for vec in adapter.embed(["hello world"]):
            assert all(0.0 <= v <= 1.0 for v in vec)

    def test_default_embedding_dim_is_768(self) -> None:
        adapter = _MockLangChainEmbeddingAdapter()
        assert adapter.embedding_dim == 768

    def test_satisfies_embedding_port_protocol(self) -> None:
        from mcp_server.application.ports.embedding import EmbeddingPort

        adapter = _MockLangChainEmbeddingAdapter()
        assert isinstance(adapter, EmbeddingPort)


# ---------------------------------------------------------------------------
# create_langchain_embedding factory
# ---------------------------------------------------------------------------


class TestCreateLangChainEmbedding:
    """Empty ``api_key`` flips the factory to the deterministic mock."""

    def test_returns_real_adapter_when_api_key_present(self) -> None:
        # Even a non-empty dummy key constructs the real adapter.
        adapter = create_langchain_embedding(api_key="dummy")
        assert isinstance(adapter, LangChainEmbeddingAdapter)
        assert adapter.embedding_dim == 768

    def test_returns_mock_when_api_key_is_empty(self) -> None:
        adapter = create_langchain_embedding(api_key="")
        assert isinstance(adapter, _MockLangChainEmbeddingAdapter)
        assert adapter.embedding_dim == 768

    def test_returns_mock_when_api_key_is_whitespace(self) -> None:
        adapter = create_langchain_embedding(api_key="   \n\t  ")
        assert isinstance(adapter, _MockLangChainEmbeddingAdapter)

    def test_passes_through_embedding_dim(self) -> None:
        adapter = create_langchain_embedding(api_key="dummy", embedding_dim=1024)
        assert isinstance(adapter, LangChainEmbeddingAdapter)
        assert adapter.embedding_dim == 1024

    def test_mock_embedding_dim_default(self) -> None:
        adapter = create_langchain_embedding(api_key="")
        assert isinstance(adapter, _MockLangChainEmbeddingAdapter)
        assert adapter.embedding_dim == 768
