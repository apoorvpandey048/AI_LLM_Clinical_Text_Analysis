from __future__ import annotations

import time
from typing import Any, Callable, Awaitable

from app.llm.client import LLMClient, LLMResponse


class StubLLMClient(LLMClient):
    """A minimal LLM client used for local testing when real LLM isn't available.

    It returns deterministic, small JSON responses and supports streaming via
    invoking `on_token` with token fragments.
    """

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
    ) -> LLMResponse:
        start = time.time()
        content = '{"stub": true, "prompt_preview": ' + repr(prompt[:120]) + '}'
        parsed = None
        try:
            parsed = self._try_parse_json(content)
        except Exception:
            parsed = None

        duration_ms = int((time.time() - start) * 1000)
        return LLMResponse(
            content=content,
            parsed_json=parsed,
            tokens_input=1,
            tokens_output=1,
            model="stub",
            duration_ms=duration_ms,
            success=True,
            error=None,
        )

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: int = 600,
        on_token: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        # simple streaming: send a few token fragments to callback
        import asyncio

        fragments = ["{\"stub\": ", "true, ", "\"prompt_preview\": ", repr(prompt[:120]), "}"]
        start = time.time()
        if on_token:
            for frag in fragments:
                await on_token(frag)
                await asyncio.sleep(0.01)

        content = ''.join(fragments)
        parsed = self._try_parse_json(content)
        duration_ms = int((time.time() - start) * 1000)
        return LLMResponse(
            content=content,
            parsed_json=parsed,
            tokens_input=1,
            tokens_output=len(fragments),
            model="stub",
            duration_ms=duration_ms,
            success=True,
            error=None,
        )

    async def health_check(self) -> bool:
        return True
