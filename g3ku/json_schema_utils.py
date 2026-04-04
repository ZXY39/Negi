"""Utilities for preserving and presenting raw JSON Schema contracts."""

from __future__ import annotations

import copy
import re
from typing import Any, Literal

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, create_model

_RAW_PARAMETERS_SCHEMA_ATTR = "_g3ku_raw_parameters_schema"
_DEFAULT_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}
_FIELD_EXAMPLES: dict[str, str] = {
    "path": "/absolute/path/to/example.txt",
    "url": "https://example.com",
    "tool_id": "example_tool",
    "skill_id": "example_skill",
    "query": "example search query",
    "search_query": "example search query",
    "statement": "Example normalized statement.",
    "source_excerpt": "Exact user text excerpt supporting this value.",
    "key": "example_key",
    "value": "example value",
    "task": "Describe the task to run.",
    "core_requirement": "State the core requirement in one sentence.",
}


def normalize_object_json_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    payload = copy.deepcopy(schema) if isinstance(schema, dict) else {}
    if str(payload.get("type") or "object").strip() != "object":
        return dict(_DEFAULT_OBJECT_SCHEMA)
    properties = payload.get("properties")
    required = payload.get("required")
    payload["properties"] = dict(properties) if isinstance(properties, dict) else {}
    payload["required"] = list(required) if isinstance(required, list) else []
    return payload


class _PydanticSchemaBuilder:
    def __init__(self, root_name: str) -> None:
        self._root_name = self._pascal(root_name) or "ToolArgs"
        self._used_names: set[str] = set()

    @staticmethod
    def _pascal(value: str) -> str:
        parts = [part for part in re.split(r"[^0-9A-Za-z]+", str(value or "")) if part]
        return "".join(part[:1].upper() + part[1:] for part in parts)

    def _unique_model_name(self, path: list[str]) -> str:
        pieces = [self._root_name, *[self._pascal(item) or "Item" for item in list(path or [])]]
        candidate = "".join(pieces) or self._root_name
        if candidate not in self._used_names:
            self._used_names.add(candidate)
            return candidate
        index = 2
        while f"{candidate}{index}" in self._used_names:
            index += 1
        unique = f"{candidate}{index}"
        self._used_names.add(unique)
        return unique

    @staticmethod
    def _normalized_type(schema: dict[str, Any] | None) -> tuple[Any, bool]:
        if not isinstance(schema, dict):
            return None, False
        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            non_null_types = [item for item in schema_type if item != "null"]
            if len(non_null_types) == 1:
                return non_null_types[0], "null" in schema_type
            return None, "null" in schema_type
        return schema_type, False

    @staticmethod
    def _field_default(schema: dict[str, Any], *, required: bool) -> Any:
        if "default" in schema:
            return copy.deepcopy(schema.get("default"))
        return ... if required else None

    def _annotation_for(self, schema: dict[str, Any] | None, *, path: list[str]) -> Any:
        if not isinstance(schema, dict):
            return Any
        schema_type, nullable = self._normalized_type(schema)
        enum_values = schema.get("enum")
        if isinstance(enum_values, list) and enum_values:
            annotation = Literal.__getitem__(tuple(enum_values))
            return annotation | None if nullable else annotation

        annotation: Any
        if schema_type == "string":
            annotation = str
        elif schema_type == "integer":
            annotation = int
        elif schema_type == "number":
            annotation = float
        elif schema_type == "boolean":
            annotation = bool
        elif schema_type == "array":
            item_annotation = self._annotation_for(schema.get("items"), path=[*path, "item"])
            annotation = list[item_annotation]
        elif schema_type == "object":
            properties = schema.get("properties")
            if isinstance(properties, dict) and properties:
                annotation = self._object_model(schema, path=path)
            elif isinstance(schema.get("additionalProperties"), dict):
                value_annotation = self._annotation_for(
                    schema.get("additionalProperties"),
                    path=[*path, "value"],
                )
                annotation = dict[str, value_annotation]
            else:
                annotation = dict[str, Any]
        else:
            annotation = Any
        return annotation | None if nullable else annotation

    def _object_model(self, schema: dict[str, Any], *, path: list[str]) -> type[BaseModel]:
        properties = dict(schema.get("properties") or {})
        required = {
            str(item or "").strip()
            for item in list(schema.get("required") or [])
            if str(item or "").strip()
        }
        fields: dict[str, tuple[Any, Any]] = {}
        for key, prop in properties.items():
            if not isinstance(prop, dict):
                prop = {}
            description = str(prop.get("description") or "").strip()
            annotation = self._annotation_for(prop, path=[*path, str(key)])
            default = self._field_default(prop, required=str(key) in required)
            field = Field(default=default, description=description) if description else default
            fields[str(key)] = (annotation, field)
        return create_model(
            self._unique_model_name(path),
            __config__=ConfigDict(extra="allow"),
            **fields,
        )

    def build(self, schema: dict[str, Any] | None) -> type[BaseModel]:
        normalized = normalize_object_json_schema(schema)
        return self._object_model(normalized, path=[])


def build_args_schema_model(tool_name: str, schema: dict[str, Any] | None) -> type[BaseModel]:
    return _PydanticSchemaBuilder(f"{tool_name}_args").build(schema)


def attach_raw_parameters_schema(tool: Any, schema: dict[str, Any] | None) -> Any:
    if tool is not None:
        setattr(tool, _RAW_PARAMETERS_SCHEMA_ATTR, normalize_object_json_schema(schema))
    return tool


def get_attached_raw_parameters_schema(tool: Any) -> dict[str, Any] | None:
    schema = getattr(tool, _RAW_PARAMETERS_SCHEMA_ATTR, None)
    return copy.deepcopy(schema) if isinstance(schema, dict) else None


def to_openai_tool_definition(tool: Any) -> dict[str, Any]:
    raw_schema = get_attached_raw_parameters_schema(tool)
    if raw_schema is not None:
        return {
            "type": "function",
            "function": {
                "name": str(getattr(tool, "name", "") or ""),
                "description": str(getattr(tool, "description", "") or ""),
                "parameters": raw_schema,
            },
        }
    return convert_to_openai_tool(tool)


def build_example_from_schema(schema: dict[str, Any] | None, *, field_name: str = "") -> Any:
    if not isinstance(schema, dict):
        return {}
    if "default" in schema:
        return copy.deepcopy(schema.get("default"))
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return copy.deepcopy(enum_values[0])
    schema_type, _nullable = _PydanticSchemaBuilder._normalized_type(schema)
    normalized_field = str(field_name or "").strip().lower()
    if schema_type == "object":
        result: dict[str, Any] = {}
        properties = dict(schema.get("properties") or {})
        required = {
            str(item or "").strip()
            for item in list(schema.get("required") or [])
            if str(item or "").strip()
        }
        for key, prop in properties.items():
            if str(key) not in required and "default" not in (prop if isinstance(prop, dict) else {}):
                continue
            result[str(key)] = build_example_from_schema(prop if isinstance(prop, dict) else {}, field_name=str(key))
        return result
    if schema_type == "array":
        return [build_example_from_schema(schema.get("items"), field_name=f"{normalized_field}_item")]
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return True
    if normalized_field in _FIELD_EXAMPLES:
        return _FIELD_EXAMPLES[normalized_field]
    if normalized_field.endswith("_id"):
        return f"example_{normalized_field}"
    if "path" in normalized_field:
        return _FIELD_EXAMPLES["path"]
    if "url" in normalized_field:
        return _FIELD_EXAMPLES["url"]
    return "example"


def render_parameter_contract_markdown(schema: dict[str, Any] | None) -> str:
    normalized = normalize_object_json_schema(schema)
    lines = ["## Parameter Contract"]
    required = [str(item or "").strip() for item in list(normalized.get("required") or []) if str(item or "").strip()]
    if required:
        lines.append(f"- Required top-level fields: `{', '.join(required)}`")
    else:
        lines.append("- Required top-level fields: none")
    for key, prop in dict(normalized.get("properties") or {}).items():
        lines.extend(_describe_schema_lines(prop if isinstance(prop, dict) else {}, path=str(key), required=str(key) in required))
    return "\n".join(lines).strip()


def _describe_schema_lines(schema: dict[str, Any], *, path: str, required: bool) -> list[str]:
    schema_type, _nullable = _PydanticSchemaBuilder._normalized_type(schema)
    label = _schema_type_label(schema_type, schema)
    description = str(schema.get("description") or "").strip()
    line = f"- `{path}` ({label}, {'required' if required else 'optional'})"
    if description:
        line = f"{line}: {description}"
    lines = [line]
    if schema_type == "object":
        nested_required = {
            str(item or "").strip()
            for item in list(schema.get("required") or [])
            if str(item or "").strip()
        }
        for key, prop in dict(schema.get("properties") or {}).items():
            lines.extend(
                _describe_schema_lines(
                    prop if isinstance(prop, dict) else {},
                    path=f"{path}.{key}",
                    required=str(key) in nested_required,
                )
            )
    elif schema_type == "array" and isinstance(schema.get("items"), dict):
        item_schema = dict(schema.get("items") or {})
        item_type, _nullable = _PydanticSchemaBuilder._normalized_type(item_schema)
        if item_type == "object":
            nested_required = {
                str(item or "").strip()
                for item in list(item_schema.get("required") or [])
                if str(item or "").strip()
            }
            for key, prop in dict(item_schema.get("properties") or {}).items():
                lines.extend(
                    _describe_schema_lines(
                        prop if isinstance(prop, dict) else {},
                        path=f"{path}[*].{key}",
                        required=str(key) in nested_required,
                    )
                )
        else:
            lines.extend(
                _describe_schema_lines(
                    item_schema,
                    path=f"{path}[*]",
                    required=True,
                )
            )
    return lines


def _schema_type_label(schema_type: Any, schema: dict[str, Any]) -> str:
    if schema_type == "array":
        item_type, _nullable = _PydanticSchemaBuilder._normalized_type(schema.get("items"))
        item_label = item_type or "any"
        return f"array<{item_label}>"
    if isinstance(schema.get("enum"), list) and schema.get("enum"):
        values = ", ".join(f"`{item}`" for item in list(schema.get("enum") or []))
        return f"{schema_type or 'value'} enum: {values}"
    return str(schema_type or "any")
