"""LangChain adapters for chunking, agent orchestration, and embedding.

This is the SINGLE module that wires LangChain / LangGraph into the
project. Per the 005-langchain-integration architecture decision,
chunking + agent + embedding live together here so the LangChain
surface area is small, discoverable, and replaceable in one place.

Hexagonal note: the application ports (``EmbeddingPort``, ``AgentPort``,
``ChunkingPort``) are NOT imported here — LangChain adapters are
structural Protocol matches, no inheritance. Composition root wires
them in.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_text_splitters import (
    MarkdownTextSplitter,
    PythonCodeTextSplitter,
    RecursiveCharacterTextSplitter,
)
from langgraph.prebuilt import create_react_agent

from mcp_server.application.ports.agent import AgentChunk, AgentRequest, AgentResponse
from mcp_server.application.ports.chunking import Chunk


class LangChainChunkingAdapter:
    """Split source text with language-aware LangChain splitters."""

    def __init__(self, chunk_size: int = 1500, chunk_overlap: int = 200) -> None:
        options = {
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "add_start_index": True,
        }
        self._markdown = MarkdownTextSplitter(**options)
        self._python = PythonCodeTextSplitter(**options)
        self._generic = RecursiveCharacterTextSplitter(**options)

    def chunk(self, content: str, file_path: Path) -> list[Chunk]:
        if not content:
            return []
        extension = file_path.suffix.lower()
        if extension in {".md", ".markdown"}:
            splitter = self._markdown
        elif extension == ".py":
            splitter = self._python
        else:
            splitter = self._generic
        documents = splitter.create_documents([content])
        return [
            Chunk(
                text=document.page_content,
                start_char=document.metadata["start_index"],
                end_char=document.metadata["start_index"] + len(document.page_content),
            )
            for document in documents
        ]


class _MockLangChainAgentAdapter:
    async def run(self, request: AgentRequest, tools: Sequence[Any]) -> AgentResponse:
        return AgentResponse(answer="[mock answer to: hi]")

    async def stream(
        self, request: AgentRequest, tools: Sequence[Any]
    ) -> AsyncIterator[AgentChunk]:
        """Mock stream: 5 deterministic tokens spaced 50ms apart, then DONE.

        Per the 003-playground-ui agent-streaming spec, the mock yields
        exactly the five tokens ``("Tok", "en", "ized", " mock",
        " answer")`` with ``asyncio.sleep(0.05)`` between each so the
        SSE encoder can demonstrate the streaming UX without an API key.

        The terminal ``AgentChunk(kind="done", data="")`` is yielded
        after the last token; ``AskPortfolioUseCase.astream`` consumes
        it as the trigger to emit the final ``AskPortfolioResult``.
        """
        for token in ("Tok", "en", "ized", " mock", " answer"):
            await asyncio.sleep(0.05)
            yield AgentChunk(kind="token", data=token)
        yield AgentChunk(kind="done", data="")


class LangChainAgentAdapter:
    """Run sibling tools through a LangGraph ReAct agent."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        *,
        llm: Any | None = None,
        max_output_tokens: int = 600,
    ) -> None:
        # ``max_output_tokens=600`` is the 003-playground-ui
        # llm-prompt-discipline cap (Decision #12 — short-first
        # invariant). The spec's original Pydantic AI field
        # ``UsageLimits.response_tokens_limit`` no longer applies after
        # 005-langchain-integration migrated to LangGraph; the
        # LangChain-native equivalent is the field on the chat model
        # itself. REL-8 finding from PR1's reliability review.
        self._llm = llm or ChatGoogleGenerativeAI(
            model=model,
            api_key=api_key,
            max_output_tokens=max_output_tokens,
        )

    async def run(self, request: AgentRequest, tools: Sequence[Any]) -> AgentResponse:
        tool_functions = [getattr(tool, "fn", tool) for tool in tools]
        agent = create_react_agent(self._llm, tool_functions)
        messages = [*(request.history or []), {"role": "user", "content": request.question}]
        result = await agent.ainvoke(
            {"messages": messages},
            config={"recursion_limit": request.max_tool_calls * 2 + 1},
        )
        result_messages = result["messages"]
        tool_calls = [
            tool_call
            for message in result_messages
            for tool_call in getattr(message, "tool_calls", [])
        ]
        return AgentResponse(
            answer=str(result_messages[-1].content),
            tool_calls=tool_calls,
        )

    async def stream(
        self, request: AgentRequest, tools: Sequence[Any]
    ) -> AsyncIterator[AgentChunk]:
        """Stream the LangGraph ReAct agent's tokens, one ``AgentChunk`` per ``AIMessageChunk``.

        Implements the 003-playground-ui agent-streaming spec: uses
        ``stream_mode="messages"`` (the documented stable surface; see
        ADR-003) and yields one token chunk per ``AIMessageChunk``
        event. Non-AI messages (HumanMessage, ToolMessage) are
        silently filtered so the chat shows only the assistant's
        prose. Chunks with ``content is None`` (LangGraph emits
        these during tool handoff) are skipped to avoid
        ``str(None) == 'None'`` leaking into the SSE stream (REL-12).

        A terminal ``AgentChunk(kind="done", data="")`` is yielded
        after the agent finishes, mirroring the mock adapter so
        ``AskPortfolioUseCase.astream`` has a single termination
        contract for both adapters.
        """
        tool_functions = [getattr(tool, "fn", tool) for tool in tools]
        agent = create_react_agent(self._llm, tool_functions)
        messages = [*(request.history or []), {"role": "user", "content": request.question}]
        async for message, _meta in agent.astream(
            {"messages": messages},
            config={"recursion_limit": request.max_tool_calls * 2 + 1},
            stream_mode="messages",
        ):
            if not isinstance(message, AIMessageChunk):
                continue
            if message.content is None:
                # Skip tool-handoff chunks — str(None) is 'None'.
                continue
            yield AgentChunk(kind="token", data=str(message.content))
        yield AgentChunk(kind="done", data="")


def create_langchain_adapter(
    api_key: str = "",
    chunk_size: int = 1500,
    chunk_overlap: int = 200,
) -> LangChainChunkingAdapter:
    return LangChainChunkingAdapter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)


def create_langchain_agent(
    api_key: str,
    model: str = "gemini-2.0-flash",
) -> LangChainAgentAdapter | _MockLangChainAgentAdapter:
    if not api_key.strip():
        return _MockLangChainAgentAdapter()
    return LangChainAgentAdapter(api_key=api_key, model=model)


class LangChainEmbeddingAdapter:
    """EmbeddingPort implementation backed by LangChain's Google Gemini embeddings.

    The adapter delegates to ``langchain_google_genai.GoogleGenerativeAIEmbeddings``
    which wraps the new ``google-genai`` SDK under the hood. Batching is
    handled by the LangChain class itself (its ``embed_documents`` accepts
    a ``list[str]`` and slices into <=100-text batches automatically).

    Args:
        api_key: Google Gemini API key. Empty string raises ``ValueError``
            from the LangChain pydantic model validation — callers should
            use :func:`create_langchain_embedding` to opt into the mock.
        model: Gemini embedding model identifier. Default
            ``"text-embedding-004"`` (768-dim, free tier).
        embedding_dim: Declared output dimension. The LangChain client
            doesn't surface this directly, so we cache it as an
            attribute that :class:`IndexProjectUseCase` reads via
            ``getattr(adapter, "embedding_dim", 768)``.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-004",
        embedding_dim: int = 768,
    ) -> None:
        self._embeddings = GoogleGenerativeAIEmbeddings(
            model=model,
            google_api_key=api_key,
        )
        self.embedding_dim = embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via LangChain's ``embed_documents``.

        One round-trip per call (LangChain batches up to 100 texts
        internally). Order is preserved: ``result[i]`` corresponds to
        ``texts[i]``.
        """
        return self._embeddings.embed_documents(texts)


class _MockLangChainEmbeddingAdapter:
    """Deterministic EmbeddingPort mock backed by SHA-256 of the input text.

    Per the 005-langchain-integration spec the mock variant lives next
    to its real sibling in this single file. Each 4-byte chunk of the
    SHA-256 digest becomes a float in ``[0, 1]`` via ``v / 2**32``.
    Same input → same vector across processes (no SDK, no network).

    Used by ``--mock-gemini`` and tests that need a real ``EmbeddingPort``
    instance without standing up the LangChain client.
    """

    def __init__(self, embedding_dim: int = 768) -> None:
        self.embedding_dim = embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return ``embedding_dim``-float deterministic vectors for each text.

        SHA-256 returns 32 bytes; for ``embedding_dim * 4`` bytes we
        tile the digest until we have enough material, then truncate.
        """
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        needed_bytes = self.embedding_dim * 4
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Tile the digest (32 bytes) until we have enough material,
        # then truncate. SHA-256 has uniform distribution so tiling
        # is safe for deterministic-vector generation.
        buf = digest * (needed_bytes // len(digest) + 1)
        buf = buf[:needed_bytes]
        ints = [int.from_bytes(buf[i : i + 4], "big") for i in range(0, len(buf), 4)]
        return [(v / 2**32) for v in ints[: self.embedding_dim]]


def create_langchain_embedding(
    api_key: str = "",
    model: str = "text-embedding-004",
    embedding_dim: int = 768,
) -> LangChainEmbeddingAdapter | _MockLangChainEmbeddingAdapter:
    """Create the LangChain embedding adapter. Mock when ``api_key`` is empty.

    Mirrors :func:`create_langchain_agent`: an empty/whitespace API key
    flips the factory to the deterministic mock variant so the build
    always succeeds (auto-fallback at the CLI, ``--mock-gemini`` mode,
    tests without a network).
    """
    if not api_key.strip():
        return _MockLangChainEmbeddingAdapter(embedding_dim=embedding_dim)
    return LangChainEmbeddingAdapter(api_key=api_key, model=model, embedding_dim=embedding_dim)
