import time
import logging
from typing import List, Callable, Any, Optional, Dict
from pydantic import BaseModel
from google import genai
from google.genai import types
from google.genai.errors import APIError

logger = logging.getLogger("autodev.llm")


class LLMResponse(BaseModel):
    text: str
    tool_calls: List[Dict[str, Any]] = []
    model_name: str
    raw_content: Optional[Any] = None

    model_config = {"arbitrary_types_allowed": True}


class GeminiClient:
    def __init__(self, api_key: str, model_name: str = "gemini-3.5-flash"):
        self.model_name = model_name
        try:
            self.client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=60_000),
            )
        except Exception as e:
            logger.error(f"Failed to initialize Gemini Client: {e}")
            raise RuntimeError("Failed to initialize Google GenAI SDK.") from e

    def generate(
        self,
        contents: Any,
        system_instruction: Optional[str] = None,
        tools: Optional[List[Callable[..., Any]]] = None,
        tool_config: Optional[Any] = None,
        max_retries: int = 5,
        max_timeout_retries: int = 3,
        backoff_factor: float = 2.0,
        max_delay: float = 30.0,
    ) -> LLMResponse:
        """Calls the Gemini API to generate content, with retries on rate limits or API errors."""
        config_args: Dict[str, Any] = {
            "temperature": 0.0,
        }
        if system_instruction:
            config_args["system_instruction"] = system_instruction
        if tools:
            config_args["tools"] = tools
        if tool_config:
            config_args["tool_config"] = tool_config

        config = types.GenerateContentConfig(**config_args)

        delay = 1.0
        timeout_count = 0
        for attempt in range(1, max_retries + 1):
            try:
                logger.debug(
                    f"Calling Gemini API (model={self.model_name}), attempt {attempt}..."
                )
                response = self.client.models.generate_content(
                    model=self.model_name, contents=contents, config=config
                )

                text_parts = []
                if response.candidates:
                    for candidate in response.candidates:
                        if candidate.content and candidate.content.parts:
                            for part in candidate.content.parts:
                                if part.text:
                                    text_parts.append(part.text)
                text_content = "".join(text_parts)

                tool_calls = []
                if response.function_calls:
                    for call in response.function_calls:
                        tool_calls.append({"name": call.name, "args": call.args})

                raw_content = None
                if response.candidates and len(response.candidates) > 0:
                    raw_content = response.candidates[0].content

                return LLMResponse(
                    text=text_content,
                    tool_calls=tool_calls,
                    model_name=self.model_name,
                    raw_content=raw_content,
                )

            except APIError as e:
                # 503/504 are gateway timeouts — treat them like read timeouts with separate budget
                if e.code in (503, 504):
                    timeout_count += 1
                    logger.warning(
                        f"Gemini API gateway timeout (code {e.code}), #{timeout_count}/{max_timeout_retries}."
                    )
                    if timeout_count >= max_timeout_retries:
                        raise RuntimeError(
                            f"Gemini API timed out {timeout_count} times (last: HTTP {e.code}). "
                            f"The model may be overloaded or the request too large."
                        ) from e
                    sleep_time = min(
                        delay * (backoff_factor ** (timeout_count - 1)), max_delay
                    )
                    logger.warning(f"Timeout retry in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                # Rate limits (429) and other transient server errors (500, 502)
                elif e.code in (429, 500, 502) and attempt < max_retries:
                    sleep_time = min(
                        delay * (backoff_factor ** (attempt - 1)), max_delay
                    )
                    logger.warning(
                        f"Gemini API rate limit or transient error (code {e.code}). Retrying in {sleep_time:.2f}s..."
                    )
                    time.sleep(sleep_time)
                else:
                    logger.error(f"Gemini API Error (code {e.code}): {e.message}")
                    raise RuntimeError(f"Gemini API Error: {e.message}") from e
            except Exception as e:
                err_str = str(e).lower()
                is_timeout = "timed out" in err_str or "timeout" in err_str

                if is_timeout:
                    timeout_count += 1
                    logger.error(
                        f"Gemini API timeout #{timeout_count}/{max_timeout_retries}: {e}"
                    )
                    if timeout_count >= max_timeout_retries:
                        raise RuntimeError(
                            f"Gemini API timed out {timeout_count} times consecutively. "
                            f"The model may be overloaded or the request too large."
                        ) from e
                    sleep_time = min(
                        delay * (backoff_factor ** (timeout_count - 1)), max_delay
                    )
                    logger.warning(f"Timeout retry in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                elif attempt < max_retries:
                    sleep_time = min(
                        delay * (backoff_factor ** (attempt - 1)), max_delay
                    )
                    logger.warning(
                        f"Unexpected error calling Gemini API: {e}. Retrying in {sleep_time:.2f}s..."
                    )
                    time.sleep(sleep_time)
                else:
                    raise RuntimeError(
                        f"Failed to communicate with Gemini API: {e}"
                    ) from e

        raise RuntimeError(
            "Failed to generate content from Gemini API after maximum retries."
        )
