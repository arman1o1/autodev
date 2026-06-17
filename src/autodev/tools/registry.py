import logging
import inspect
from dataclasses import dataclass
from typing import Callable, Dict, Any, List

logger = logging.getLogger("autodev.tools")


@dataclass
class ToolResult:
    success: bool
    output: str
    error: str | None = None


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Callable[..., Any]] = {}

    def register(self, func: Callable[..., Any], name: str = None):
        """Registers a function as a tool."""
        tool_name = name or func.__name__
        self._tools[tool_name] = func
        logger.debug(f"Registered tool: {tool_name}")

    def register_all_from_class(self, instance: Any):
        """Registers all public methods of an class instance as tools."""
        for attr_name in dir(instance):
            if attr_name.startswith("_"):
                continue
            attr = getattr(instance, attr_name)
            if inspect.ismethod(attr):
                self.register(attr, name=attr_name)

    def execute(self, tool_name: str, args: Dict[str, Any]) -> ToolResult:
        """Executes a tool by name with the given arguments."""
        if tool_name not in self._tools:
            err_msg = f"Tool '{tool_name}' is not registered."
            logger.error(err_msg)
            return ToolResult(success=False, output="", error=err_msg)

        func = self._tools[tool_name]
        logger.info(f"Executing tool '{tool_name}' with args: {args}")
        try:
            # Bind arguments to handle potential type casting or validation if needed
            sig = inspect.signature(func)
            bound = sig.bind(**args)
            bound.apply_defaults()

            # Execute the tool function
            result = func(*bound.args, **bound.kwargs)

            # Convert result to string representation
            output_str = str(result) if result is not None else "Success"
            return ToolResult(success=True, output=output_str)
        except Exception as e:
            logger.error(f"Error executing tool '{tool_name}': {e}", exc_info=True)
            return ToolResult(success=False, output="", error=str(e))

    def get_tools_list(self) -> List[Callable[..., Any]]:
        """Returns list of registered tool callables for passing to Gemini SDK."""
        return list(self._tools.values())
