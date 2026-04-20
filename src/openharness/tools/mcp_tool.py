"""MCP tool adapters."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field, create_model

from openharness.mcp.client import McpClientManager, McpServerNotConnectedError
from openharness.mcp.types import McpToolInfo
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

log = logging.getLogger(__name__)


class McpToolAdapter(BaseTool):
    """Expose one MCP tool as a normal OpenHarness tool."""

    def __init__(self, manager: McpClientManager, tool_info: McpToolInfo) -> None:
        self._manager = manager
        self._tool_info = tool_info
        server_segment = _sanitize_tool_segment(tool_info.server_name)
        tool_segment = _sanitize_tool_segment(tool_info.name)
        self.name = f"mcp__{server_segment}__{tool_segment}"
        self.description = tool_info.description or f"MCP tool {tool_info.name}"
        self.input_model = _input_model_from_schema(self.name, tool_info.input_schema)

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        del context
        try:
            output = await self._manager.call_tool(
                self._tool_info.server_name,
                self._tool_info.name,
                arguments.model_dump(mode="json", exclude_none=True),
            )
            return ToolResult(output=output)
        except McpServerNotConnectedError as exc:
            log.warning(
                "mcp_tool_call_failed_reconnecting server=%s tool=%s error=%s",
                self._tool_info.server_name,
                self._tool_info.name,
                exc,
            )
        # First call failed — reconnect once and retry
        try:
            await self._manager.reconnect_all()
        except Exception as reconnect_exc:
            log.warning(
                "mcp_reconnect_failed server=%s error=%s",
                self._tool_info.server_name,
                reconnect_exc,
            )
            return ToolResult(
                output=f"MCP server '{self._tool_info.server_name}' reconnect failed: {reconnect_exc}",
                is_error=True,
            )
        try:
            output = await self._manager.call_tool(
                self._tool_info.server_name,
                self._tool_info.name,
                arguments.model_dump(mode="json", exclude_none=True),
            )
            log.warning(
                "mcp_tool_call_retry_success server=%s tool=%s",
                self._tool_info.server_name,
                self._tool_info.name,
            )
            return ToolResult(output=output)
        except McpServerNotConnectedError as exc:
            return ToolResult(output=str(exc), is_error=True)


_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _input_model_from_schema(tool_name: str, schema: dict[str, object]) -> type[BaseModel]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return create_model(f"{tool_name.title()}Input")

    fields = {}
    required = set(schema.get("required", [])) if isinstance(schema.get("required", []), list) else set()
    for key in properties:
        prop = properties[key] if isinstance(properties[key], dict) else {}
        py_type = _JSON_TYPE_MAP.get(str(prop.get("type", "")), object)
        if key in required:
            fields[key] = (py_type, Field(default=...))
        else:
            fields[key] = (py_type | None, Field(default=None))
    return create_model(f"{tool_name.title().replace('-', '_')}Input", **fields)


def _sanitize_tool_segment(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not sanitized:
        return "tool"
    if not sanitized[0].isalpha():
        return f"mcp_{sanitized}"
    return sanitized
