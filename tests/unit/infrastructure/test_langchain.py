from pathlib import Path

from mcp_server.infrastructure.langchain import LangChainChunkingAdapter


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
