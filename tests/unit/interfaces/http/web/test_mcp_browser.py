"""Unit tests for ``src/mcp_server/interfaces/http/web/mcp_browser.py``.

The ``/mcp-ui`` browser introspects tool schemas via ``mcp.list_tools()``
and renders one form per tool. The serialization layer (``_serialize_tools``)
must:

* Read the JSON Schema for each tool from the FastMCP tool object.
  FastMCP 3.4.6 renamed the ``inputSchema`` attribute to ``parameters``;
  the serializer must accept the new name AND fall back to the old
  one (back-compat with FastMCP < 3.4).
* Flatten each schema's ``properties`` into a form-friendly list
  (``_fields_from_schema``) so the Jinja template can render native
  inputs (text / number / checkbox) instead of falling back to a JSON
  textarea.
* Translate the tool description and per-field labels/descriptions to
  Spanish (UI-only override map — Python docstrings stay English).

These tests build tiny ``FunctionTool``-shaped fakes rather than
bootstrapping the full FastMCP composition root. They are the
RED-step driving tests for the ``mcp-ui-spanish-override`` change.
"""

from __future__ import annotations

import json
from typing import Any

from mcp_server.interfaces.http.web import mcp_browser
from mcp_server.interfaces.http.web.mcp_browser import (
    _fields_from_schema,
    _serialize_tools,
)

# ---------------------------------------------------------------------------
# Fakes — mimic FastMCP ``FunctionTool`` (3.4.6+) and legacy ``inputSchema``
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal stand-in for ``mcp.server.fastmcp.tools.base.FunctionTool``.

    The real class exposes ``name``, ``description``, and (since
    FastMCP 3.4.6) ``parameters`` — a JSON Schema dict (or pydantic
    model). Older versions used ``inputSchema`` instead. Tests construct
    whichever attribute set they want to exercise.
    """

    def __init__(
        self,
        *,
        name: str = "fake_tool",
        description: str = "",
        parameters: Any = None,
        input_schema: Any = None,
    ) -> None:
        self.name = name
        self.description = description
        if parameters is not None:
            self.parameters = parameters
        if input_schema is not None:
            self.inputSchema = input_schema


# Verified schemas (mirrored from FastMCP 3.4.6 introspection; see
# the change brief for the source-of-truth dump).
SEARCH_CODE_PARAMETERS: dict[str, Any] = {
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural-language query. MUST be non-empty.",
        },
        "top_k": {
            "type": "integer",
            "default": 5,
            "description": "Maximum results to return (capped at 50 by the use case).",
        },
        "project_id": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "description": "Optional scope filter — restrict results to one project.",
        },
    },
    "required": ["query"],
    "type": "object",
}

EXPLAIN_ARCH_PARAMETERS: dict[str, Any] = {
    "properties": {
        "project_id": {"type": "string"},
        "max_tokens": {"type": "integer", "default": 500},
    },
    "required": ["project_id"],
    "type": "object",
}


# ---------------------------------------------------------------------------
# Task 1 — FastMCP 3.4.6 attribute rename: ``inputSchema`` → ``parameters``
# ---------------------------------------------------------------------------


class TestSerializeToolsParametersAttribute:
    """FastMCP 3.4.6 renamed ``inputSchema`` to ``parameters``. The
    serializer must read the new attribute; legacy ``inputSchema``
    remains supported as a fallback; when both are present the new
    one wins.
    """

    def test_reads_dot_parameters_dict_and_populates_fields(self) -> None:
        """A tool exposing ``.parameters`` as a JSON Schema dict MUST
        yield a serialized entry whose ``fields`` list contains the
        schema's properties with the expected per-field shape.

        Uses an unknown tool name so the Spanish override map does not
        influence the assertion — this test is about attribute access,
        not translation.
        """
        tool = _FakeTool(
            name="generic_search",
            description="English description.",
            parameters=SEARCH_CODE_PARAMETERS,
        )
        [serialized] = _serialize_tools([tool])

        assert serialized["name"] == "generic_search"
        fields = serialized["fields"]
        assert len(fields) == 3, f"expected 3 fields (query, top_k, project_id); got {len(fields)}"
        field_names = {f["name"] for f in fields}
        assert field_names == {"query", "top_k", "project_id"}

        by_name = {f["name"]: f for f in fields}
        # Per-field shape.
        assert by_name["query"]["label"] == "Query"
        assert by_name["query"]["type"] == "text"
        assert by_name["query"]["required"] is True
        assert by_name["query"]["description"] == ("Natural-language query. MUST be non-empty.")

        assert by_name["top_k"]["type"] == "number"
        assert by_name["top_k"]["required"] is False
        assert by_name["top_k"]["default"] == 5

        # Nullable / multi-type: first non-null wins → "string" → text input.
        assert by_name["project_id"]["type"] == "text"
        assert by_name["project_id"]["required"] is False

        # The raw schema is also exposed for the "inputSchema en bruto"
        # disclosure block.
        assert json.loads(serialized["input_schema_json"]) == SEARCH_CODE_PARAMETERS

    def test_legacy_dot_input_schema_still_works_as_fallback(self) -> None:
        """A tool that only exposes the legacy ``.inputSchema``
        attribute MUST still serialize correctly (back-compat with
        FastMCP < 3.4). Uses an unknown tool name so the Spanish
        override map does not skew the field count.
        """
        tool = _FakeTool(
            name="generic_search",
            description="legacy tool",
            input_schema=SEARCH_CODE_PARAMETERS,
        )
        [serialized] = _serialize_tools([tool])

        fields = serialized["fields"]
        assert len(fields) == 3, (
            "legacy .inputSchema must keep working — empty fields would "
            "re-introduce the regression this change is fixing"
        )
        assert {f["name"] for f in fields} == {"query", "top_k", "project_id"}

    def test_dot_parameters_takes_precedence_when_both_present(self) -> None:
        """When a tool exposes BOTH ``.parameters`` AND ``.inputSchema``,
        ``.parameters`` wins (it's the canonical FastMCP 3.4.6 attribute
        and matches what the live server actually validates against).
        """
        tool = _FakeTool(
            name="generic_search",
            parameters=SEARCH_CODE_PARAMETERS,
            input_schema={"properties": {}, "required": [], "type": "object"},
        )
        [serialized] = _serialize_tools([tool])

        # .parameters had 3 fields; .inputSchema had 0. We must see 3.
        assert len(serialized["fields"]) == 3, (
            ".parameters must win over .inputSchema when both are present"
        )


# ---------------------------------------------------------------------------
# Task 2 — Spanish UI-only field label / description overrides
# ---------------------------------------------------------------------------


class TestFieldTranslationOverrides:
    """Field labels and descriptions come from English docstrings. A
    UI-only override map in ``mcp_browser`` translates them to Spanish
    without changing any Python source.
    """

    def test_search_code_field_labels_and_descriptions_are_spanish(self) -> None:
        tool = _FakeTool(
            name="search_code",
            parameters=SEARCH_CODE_PARAMETERS,
        )
        [serialized] = _serialize_tools([tool])
        by_name = {f["name"]: f for f in serialized["fields"]}

        assert by_name["query"]["label"] == "Consulta"
        assert by_name["query"]["description"] == (
            "Texto en lenguaje natural. No puede estar vacío."
        )
        assert by_name["top_k"]["label"] == "Cantidad de resultados"
        assert by_name["top_k"]["description"] == ("Máximo de resultados a devolver (tope 50).")
        assert by_name["project_id"]["label"] == "ID del proyecto"
        assert by_name["project_id"]["description"] == (
            "Filtro opcional — limita la búsqueda a un proyecto."
        )

    def test_explain_architecture_project_id_has_spanish_label(self) -> None:
        """The schema has no English description for ``project_id`` —
        the override must still produce a Spanish description."""
        tool = _FakeTool(
            name="explain_architecture",
            parameters=EXPLAIN_ARCH_PARAMETERS,
        )
        [serialized] = _serialize_tools([tool])
        [project_id_field] = [f for f in serialized["fields"] if f["name"] == "project_id"]
        assert project_id_field["label"] == "ID del proyecto"
        assert project_id_field["description"] == ("Identificador del proyecto a explicar.")

    def test_unknown_tool_falls_back_without_crashing(self) -> None:
        """A hypothetical tool with an unknown name + a field that has
        no override MUST NOT crash — labels default to title-case of
        the field name and descriptions default to whatever the
        schema declares.
        """
        weird_schema: dict[str, Any] = {
            "properties": {
                "weird_field": {
                    "type": "string",
                    "description": "English description that has no override.",
                }
            },
            "required": ["weird_field"],
            "type": "object",
        }
        tool = _FakeTool(
            name="hypothetical_tool",
            parameters=weird_schema,
        )
        [serialized] = _serialize_tools([tool])
        [field] = serialized["fields"]
        assert field["label"] == "Weird Field", (
            "default label must be the title-cased field name when no override exists"
        )
        assert field["description"] == ("English description that has no override.")


# ---------------------------------------------------------------------------
# Task 3 — Tool-level description overrides
# ---------------------------------------------------------------------------


class TestToolDescriptionTranslationOverride:
    """The ``<p>{{ tool.description }}</p>`` block on the rendered page
    also comes from the English docstring. Override per tool name.
    """

    def test_search_code_description_is_spanish(self) -> None:
        tool = _FakeTool(
            name="search_code",
            description="Semantic search over the indexed code chunks...",
            parameters=SEARCH_CODE_PARAMETERS,
        )
        [serialized] = _serialize_tools([tool])
        assert serialized["description"] == (
            "Búsqueda semántica sobre los chunks de código indexados por el pipeline de preindex."
        )

    def test_unknown_tool_description_falls_back_to_english(self) -> None:
        tool = _FakeTool(
            name="hypothetical_tool",
            description="Some English description.",
            parameters={"properties": {}, "required": [], "type": "object"},
        )
        [serialized] = _serialize_tools([tool])
        assert serialized["description"] == "Some English description."


# ---------------------------------------------------------------------------
# Smoke: the override map is wired and discoverable
# ---------------------------------------------------------------------------


def test_translation_maps_are_present_and_non_empty() -> None:
    """The override constants must exist on the module (so a future
    contributor adding a new tool can extend them).
    """
    assert hasattr(mcp_browser, "_FIELD_TRANSLATIONS")
    assert hasattr(mcp_browser, "_TOOL_TRANSLATIONS")
    # Every tool that ships today has at least one field override OR
    # an empty schema (list_projects) — but list_projects MUST be
    # listed in the tool override map so its Spanish description
    # renders.
    expected_tools = {
        "list_projects",
        "search_code",
        "explain_architecture",
        "summarize_readme",
        "get_architecture_diagram",
        "ask_portfolio",
    }
    assert expected_tools <= set(mcp_browser._TOOL_TRANSLATIONS)


def test_fields_from_schema_accepts_optional_tool_name_kwarg() -> None:
    """``_fields_from_schema`` must accept ``tool_name`` as a kwarg so
    the override lookup can be scoped per tool. The kwarg is optional
    — calling without it must remain backward-compatible (title-cased
    labels from the schema).
    """
    # Without tool_name — backward-compatible: title-cased field names.
    plain = _fields_from_schema(SEARCH_CODE_PARAMETERS)
    plain_by_name = {f["name"]: f for f in plain}
    assert plain_by_name["query"]["label"] == "Query"
    assert plain_by_name["top_k"]["label"] == "Top K"

    # With tool_name="search_code" — override kicks in.
    overridden = _fields_from_schema(SEARCH_CODE_PARAMETERS, tool_name="search_code")
    overridden_by_name = {f["name"]: f for f in overridden}
    assert overridden_by_name["query"]["label"] == "Consulta"
