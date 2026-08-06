from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class Chunk(BaseModel):
    text: str
    start_char: int
    end_char: int


class ChunkingPort(Protocol):
    """Strategy for splitting source files into chunks."""

    def chunk(self, content: str, file_path: Path) -> list[Chunk]: ...
