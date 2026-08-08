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
from mcp_server.domain.exceptions import GeminiQuotaExceededError

try:
    # ``google-genai`` (the SDK LangChain wraps) raises ``ResourceExhausted``
    # on HTTP 429. The class is importable from ``google.api_core`` which
    # is a transitive dep of ``google-genai``.
    from google.api_core.exceptions import ResourceExhausted
except ImportError:  # pragma: no cover — defensive: package always present in prod
    ResourceExhausted = None  # type: ignore[assignment,misc]

from langchain_google_genai._common import GoogleGenerativeAIError


def _is_gemini_quota_error(exc: BaseException) -> bool:
    """Detect a Gemini HTTP 429 quota error regardless of exception type.

    The raw ``google-genai`` SDK raises ``google.api_core.exceptions.ResourceExhausted``
    (with ``status_code=429``) so the type-based check is sufficient there.
    LangChain's ``GoogleGenerativeAIEmbeddings`` / ``ChatGoogleGenerativeAI``,
    however, wrap that into their own ``GoogleGenerativeAIError`` which does
    NOT expose ``status_code`` or any structured accessor — only the string
    message. We fall back to a substring match on ``RESOURCE_EXHAUSTED`` /
    ``429`` to cover the LangChain path. The substring is brittle but it's
    the only signal the LangChain wrapper exposes; if LangChain ever
    adds a structured accessor, replace the substring check.
    """
    if ResourceExhausted is not None and isinstance(exc, ResourceExhausted):
        return True
    if isinstance(exc, GoogleGenerativeAIError):
        msg = str(exc).upper()
        return "RESOURCE_EXHAUSTED" in msg or " 429 " in msg
    return False

DEFAULT_AGENT_SYSTEM_PROMPT = (
    "Eres un asistente técnico que responde preguntas sobre los proyectos "
    "de un portfolio de software. Sigue estas reglas:\n"
    "1. Responde siempre en el idioma del usuario.\n"
    "2. Tienes un presupuesto limitado de herramientas: usa como máximo "
    "2-3 llamadas a tools por respuesta. Después de obtener resultados "
    "relevantes, sintetiza la respuesta final inmediatamente.\n"
    "3. Si una tool no devuelve resultados útiles tras 2 intentos con la "
    "misma herramienta, responde con lo que sepas o di claramente que no "
    "encontraste la información.\n"
    "4. Sé conciso: respuestas cortas y técnicas, no ensayos. La salida "
    "está limitada a tokens, así que ve al grano.\n"
    "5. No inventes nombres de archivos, funciones ni APIs. Si no estás "
    "seguro, dilo.\n"
    "6. Cita rutas de archivo exactas cuando menciones código."
)


def _extract_tool_call_payload(raw_tool_call: Any) -> dict[str, Any] | None:
    """Normalize a single ``AIMessageChunk.tool_calls`` entry to the wire shape.

    LangChain emits each tool call as either a dict
    (``{"name": ..., "args": ..., "id": ...}``) or a ``ToolCall``-like
    object exposing the same attributes via attribute access. The
    adapter returns a clean ``{"name", "args", "id"}`` dict so the
    use case layer (and the SSE encoder, and the browser pill
    renderer) can rely on a single shape without sniffing for
    ``isinstance(raw, dict)``.

    Returns ``None`` for entries that lack a usable name — the caller
    must skip those, not invent one.
    """
    name: Any
    args: Any
    call_id: Any
    if isinstance(raw_tool_call, dict):
        name = raw_tool_call.get("name")
        args = raw_tool_call.get("args", {})
        call_id = raw_tool_call.get("id")
    else:
        name = getattr(raw_tool_call, "name", None)
        args = getattr(raw_tool_call, "args", {})
        call_id = getattr(raw_tool_call, "id", None)
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(args, dict):
        # LangGraph passes ``args`` as a parsed dict on tool_calls;
        # any non-dict value here would be a model/protocol surprise
        # that the UI can't render anyway. Coerce safely.
        try:
            args = dict(args) if args is not None else {}
        except (TypeError, ValueError):
            args = {}
    call_id_str = str(call_id) if call_id is not None else ""
    return {"name": name, "args": args, "id": call_id_str}


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
    def __init__(self, *, state_modifier: str = DEFAULT_AGENT_SYSTEM_PROMPT) -> None:
        # Mirror LangChainAgentAdapter's signature so tests using the
        # mock don't need extra wiring; the param is ignored by the mock.
        self._state_modifier = state_modifier

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
        model: str = "gemini-flash-latest",
        *,
        llm: Any | None = None,
        max_output_tokens: int = 1000,
        state_modifier: str = DEFAULT_AGENT_SYSTEM_PROMPT,
    ) -> None:
        # ``max_output_tokens=1000`` is the 003-playground-ui
        # llm-prompt-discipline cap (Decision #12 — short-first
        # invariant). The spec's original Pydantic AI field
        # ``UsageLimits.response_tokens_limit`` no longer applies after
        # 005-langchain-integration migrated to LangGraph; the
        # LangChain-native equivalent is the field on the chat model
        # itself. REL-8 finding from PR1's reliability review.
        # Cap was bumped from 600 → 1000 because 600 was too tight to
        # synthesize a final answer after seeing tool results on a
        # typical Spanish response — the agent ran out of tokens before
        # reaching the closing sentence.
        self._llm = llm or ChatGoogleGenerativeAI(
            model=model,
            api_key=api_key,
            max_output_tokens=max_output_tokens,
        )
        # ``state_modifier`` is the system prompt threaded into
        # LangGraph's ``create_react_agent(..., prompt=...)`` kwarg.
        # The default ``DEFAULT_AGENT_SYSTEM_PROMPT`` enforces the
        # tool-call budget + language matching required to stop the
        # "Recursion limit of 11" loop (otherwise the agent never
        # synthesizes a final answer and keeps calling tools until
        # LangGraph aborts). The public attribute name ``state_modifier``
        # mirrors the historical LangGraph kwarg; the call to
        # ``create_react_agent`` uses the current ``prompt`` kwarg.
        self._state_modifier = state_modifier

    async def run(self, request: AgentRequest, tools: Sequence[Any]) -> AgentResponse:
        tool_functions = [getattr(tool, "fn", tool) for tool in tools]
        agent = create_react_agent(self._llm, tool_functions, prompt=self._state_modifier)
        messages = [*(request.history or []), {"role": "user", "content": request.question}]
        try:
            result = await agent.ainvoke(
                {"messages": messages},
                config={"recursion_limit": request.max_tool_calls * 3 + 1},
            )
        except Exception as exc:
            if _is_gemini_quota_error(exc):
                raise GeminiQuotaExceededError(
                    f"Gemini API quota exceeded (HTTP 429): {exc}"
                ) from exc
            raise
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
        prose. Chunks with empty content shapes (``None``, empty strings, and
        empty containers) are skipped to prevent representation artifacts from
        leaking into the SSE stream. Multimodal text blocks are normalized to
        their text values; blocks without text are ignored.

        Tool-call surfacing: when an ``AIMessageChunk`` carries a non-empty
        ``tool_calls`` list, the adapter yields one
        ``AgentChunk(kind="tool_call", data={"name", "args", "id"})`` per
        tool BEFORE any token yielded for the same chunk. This mirrors
        the model's intent order — the structured tool dispatch precedes
        the prose — and lets the UI render trace pills above the body so
        recruiters can see the agent actually used RAG tools.

        A terminal ``AgentChunk(kind="done", data="")`` is yielded
        after the agent finishes, mirroring the mock adapter so
        ``AskPortfolioUseCase.astream`` has a single termination
        contract for both adapters.
        """
        tool_functions = [getattr(tool, "fn", tool) for tool in tools]
        agent = create_react_agent(self._llm, tool_functions, prompt=self._state_modifier)
        messages = [*(request.history or []), {"role": "user", "content": request.question}]
        try:
            aiter = agent.astream(
                {"messages": messages},
                config={"recursion_limit": request.max_tool_calls * 3 + 1},
                stream_mode="messages",
            )
        except Exception as exc:
            if _is_gemini_quota_error(exc):
                raise GeminiQuotaExceededError(
                    f"Gemini API quota exceeded (HTTP 429): {exc}"
                ) from exc
            raise
        try:
            async for message, _meta in aiter:
                if not isinstance(message, AIMessageChunk):
                    continue

                # Tool-call surfacing: emit one ``tool_call`` chunk per entry in
                # ``message.tool_calls`` BEFORE we attempt to tokenize the chunk's
                # content. The order matters — the UI renders the trace pills
                # above the assistant body, so the structured signal has to
                # arrive first to match the model's intent ("I want to call X,
                # then say something about it").
                raw_tool_calls = getattr(message, "tool_calls", None) or []
                for raw_tool_call in raw_tool_calls:
                    tool_payload = _extract_tool_call_payload(raw_tool_call)
                    if tool_payload is None:
                        continue
                    yield AgentChunk(kind="tool_call", data=tool_payload)

                content = message.content
                if content is None:
                    continue
                if isinstance(content, (str, list, tuple, dict)) and len(content) == 0:
                    continue
                if isinstance(content, str) and not content.strip():
                    continue

                if isinstance(content, (list, tuple)):
                    text_parts: list[str] = []
                    for block in content:
                        if isinstance(block, str):
                            text_parts.append(block)
                        elif isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text")
                            if isinstance(text, str):
                                text_parts.append(text)
                    token = "".join(text_parts)
                    if not token.strip():
                        continue
                elif isinstance(content, dict):
                    text = content.get("text") if content.get("type") == "text" else None
                    if not isinstance(text, str) or not text.strip():
                        continue
                    token = text
                else:
                    token = str(content)

                yield AgentChunk(kind="token", data=token)
        except Exception as exc:
            if _is_gemini_quota_error(exc):
                raise GeminiQuotaExceededError(
                    f"Gemini API quota exceeded (HTTP 429): {exc}"
                ) from exc
            raise
        yield AgentChunk(kind="done", data="")


def create_langchain_adapter(
    api_key: str = "",
    chunk_size: int = 1500,
    chunk_overlap: int = 200,
) -> LangChainChunkingAdapter:
    return LangChainChunkingAdapter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)


def create_langchain_agent(
    api_key: str,
    model: str = "gemini-flash-latest",
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
        model: str = "gemini-embedding-001",
        embedding_dim: int = 768,
    ) -> None:
        self._embeddings = GoogleGenerativeAIEmbeddings(
            model=model,
            google_api_key=api_key,
            output_dimensionality=embedding_dim,
        )
        self.embedding_dim = embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts via LangChain's ``embed_documents``.

        One round-trip per call (LangChain batches up to 100 texts
        internally). Order is preserved: ``result[i]`` corresponds to
        ``texts[i]``.

        Raises:
            GeminiQuotaExceededError: on HTTP 429 ``RESOURCE_EXHAUSTED``
                (daily / RPM quota exhausted). Translated from the
                underlying ``google.api_core.exceptions.ResourceExhausted``
                so the recruiter-facing message tells them to wait
                until midnight UTC / upgrade / switch keys — NOT the
                generic "service temporarily unavailable".
        """
        try:
            return self._embeddings.embed_documents(texts)
        except Exception as exc:
            if _is_gemini_quota_error(exc):
                raise GeminiQuotaExceededError(
                    f"Gemini API quota exceeded (HTTP 429): {exc}"
                ) from exc
            raise


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
    model: str = "gemini-embedding-001",
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
