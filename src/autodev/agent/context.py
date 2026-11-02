import json
import logging
import os
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from google.genai import types

logger = logging.getLogger("autodev.agent.context")


class ContextManager:
    """Manages LLM message history, sliding window pruning, token accounting, and transcript persistence."""

    def __init__(
        self,
        workspace_id: str,
        log_dir: Path,
        max_tokens: int = 80_000,
        save_run_logs: bool = True,
    ):
        self.workspace_id = workspace_id
        self.log_dir = Path(log_dir)
        self.max_tokens = max_tokens
        self.save_run_logs = save_run_logs

        self.messages: List[types.Content] = []
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.estimated_cost_usd: float = 0.0

        self.run_log_dir = self.log_dir / "runs" / self.workspace_id
        if self.save_run_logs:
            try:
                os.makedirs(self.run_log_dir, exist_ok=True)
            except Exception as e:
                logger.warning(
                    f"Failed to create run log directory {self.run_log_dir}: {e}"
                )

    def record_usage(self, prompt_tokens: int, completion_tokens: int):
        """Accumulates token counts and computes estimated USD cost."""
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        # Pricing reference: Gemini Flash ~$0.10/M input, ~$0.40/M output
        cost = (prompt_tokens / 1_000_000 * 0.10) + (
            completion_tokens / 1_000_000 * 0.40
        )
        self.estimated_cost_usd += cost

    def log_turn(
        self, role: str, content: Any, tool_calls: Optional[List[Dict[str, Any]]] = None
    ):
        """Appends a turn entry to transcript.jsonl."""
        if not self.save_run_logs:
            return
        try:
            transcript_file = self.run_log_dir / "transcript.jsonl"
            entry = {
                "timestamp": time.time(),
                "role": role,
                "content": str(content),
                "tool_calls": tool_calls or [],
            }
            with open(transcript_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.debug(f"Failed to append to transcript: {e}")

    def reset_for_mode(self, initial_user_message: str):
        """Resets the in-memory context when switching modes to prevent context bloat."""
        self.messages = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=initial_user_message)],
            )
        ]
        self.log_turn("user", initial_user_message)

    def append_model_turn(
        self, content_obj: types.Content, text: str, tool_calls: List[Dict[str, Any]]
    ):
        self.messages.append(content_obj)
        self.log_turn("model", text, tool_calls)

    def append_user_turn(self, content_obj: types.Content, text_or_summary: str):
        self.messages.append(content_obj)
        self.log_turn("user", text_or_summary)

    def get_pruned_messages(self, keep_last_n: int = 10) -> List[types.Content]:
        """Returns messages with older tool outputs truncated if message list is long."""
        if len(self.messages) <= keep_last_n:
            return list(self.messages)

        # Retain initial instruction (messages[0]) and the last keep_last_n messages
        pruned = [self.messages[0]]
        mid_messages = self.messages[1:-keep_last_n]
        tail_messages = self.messages[-keep_last_n:]

        # For intermediate messages, condense lengthy function responses
        condensed_count = 0
        for msg in mid_messages:
            if msg.role == "user" and msg.parts:
                new_parts = []
                for p in msg.parts:
                    if getattr(p, "function_response", None):
                        resp_data = p.function_response.response
                        resp_str = str(resp_data)
                        if len(resp_str) > 1000:
                            condensed_count += 1
                            short_resp = (
                                resp_str[:500]
                                + "\n... [Earlier tool output condensed] ...\n"
                                + resp_str[-200:]
                            )
                            new_parts.append(
                                types.Part(
                                    function_response=types.FunctionResponse(
                                        name=p.function_response.name,
                                        response={"result": short_resp},
                                    )
                                )
                            )
                        else:
                            new_parts.append(p)
                    else:
                        new_parts.append(p)
                pruned.append(types.Content(role=msg.role, parts=new_parts))
            else:
                pruned.append(msg)

        pruned.extend(tail_messages)
        if condensed_count > 0:
            logger.debug(
                f"Condensed {condensed_count} earlier tool outputs in context window."
            )
        return pruned
