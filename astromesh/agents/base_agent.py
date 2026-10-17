from __future__ import annotations

import json
import math
import time
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from astromesh.config import Settings
from astromesh.memory.shared_brain import OrbitalMemoryGraph

T = TypeVar("T", bound=BaseModel)


class BaseAgent:
    # All agents share one provider quota. A 429 pauses requests from the whole
    # mesh instead of letting each agent independently hammer the same limit.
    _provider_cooldown_until = 0.0
    _provider_cooldown_seconds = 30.0

    def __init__(self, name: str, settings: Settings, memory: OrbitalMemoryGraph) -> None:
        self.name, self.settings, self.memory = name, settings, memory
        self.last_latency_ms: float | None = None
        self.last_success_at: float | None = None
        self.total_latency_ms = 0.0
        self.request_count = 0
        self.successful_requests = 0
        self.inference_status = "key_missing" if not settings.inference_api_key else "ready"
        self.last_error: str | None = None
        self.last_tokens = 0
        self.last_cached_tokens = 0
        self.total_tokens = 0
        self.cached_tokens = 0
        self.call_count = 0
        self._http_client: httpx.AsyncClient | None = None

    async def structured_call(self, task: str, schema: type[T], fallback: T,
                              memory_context: str | None = None) -> T:
        api_key = self.settings.inference_api_key
        if not api_key:
            self.inference_status = "key_missing"
            self.last_error = None
            return fallback
        now = time.monotonic()
        if now < BaseAgent._provider_cooldown_until:
            remaining = max(1, math.ceil(BaseAgent._provider_cooldown_until - now))
            self.inference_status = "rate_limited"
            self.last_error = f"Provider rate limit · retry in {remaining}s"
            return fallback
        started = time.perf_counter()
        self.request_count += 1
        self.inference_status = "requesting"
        self.last_error = None
        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {"model": self.settings.llm_model, "temperature": 0.1,
                   "messages": [{"role": "system", "content": self.memory.stable_prefix()},
                                {"role": "user", "content": task + "\n\n" +
                                 (memory_context if memory_context is not None else self.memory.prompt_context())}],
                   "response_format": {"type": "json_object"}}
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=25)
        for attempt in range(2):
            try:
                if attempt:
                    payload.pop("response_format", None)
                response = await self._http_client.post(self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                                                        headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                usage = data.get("usage", {})
                self.last_tokens = int(usage.get("total_tokens", 0))
                self.last_cached_tokens = int(usage.get("prompt_tokens_details", {}).get("cached_tokens", 0))
                self.total_tokens += self.last_tokens
                self.cached_tokens += self.last_cached_tokens
                self.call_count += 1
                self.memory.record_inference(int(usage.get("prompt_tokens", 0)), self.last_cached_tokens)
                content = data["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                content = content.strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    start, end = content.find("{"), content.rfind("}")
                    if start < 0 or end < start:
                        raise
                    parsed = json.loads(content[start:end + 1])
                result = schema.model_validate(parsed)
                self.last_latency_ms = (time.perf_counter() - started) * 1000
                self.total_latency_ms += self.last_latency_ms
                self.successful_requests += 1
                self.last_success_at = time.time()
                self.inference_status = "connected"
                BaseAgent._provider_cooldown_until = 0.0
                BaseAgent._provider_cooldown_seconds = 30.0
                return result
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    retry_after = exc.response.headers.get("retry-after", "")
                    try:
                        cooldown = max(1.0, float(retry_after))
                    except ValueError:
                        cooldown = BaseAgent._provider_cooldown_seconds
                    BaseAgent._provider_cooldown_until = time.monotonic() + cooldown
                    BaseAgent._provider_cooldown_seconds = min(cooldown * 2, 300.0)
                    self.last_error = f"HTTP 429 · provider rate limit; retry in {math.ceil(cooldown)}s"
                    self.inference_status = "rate_limited"
                    break
                self.last_error = f"HTTP {exc.response.status_code}"
                if exc.response.status_code == 404:
                    self.last_error = "HTTP 404 · model ID unavailable to this account"
                    break
                continue
            except httpx.TimeoutException:
                cooldown = BaseAgent._provider_cooldown_seconds
                BaseAgent._provider_cooldown_until = time.monotonic() + cooldown
                BaseAgent._provider_cooldown_seconds = min(cooldown * 2, 300.0)
                self.last_error = f"request timed out · retry in {math.ceil(cooldown)}s"
                self.inference_status = "degraded"
                break
            except httpx.HTTPError as exc:
                self.last_error = type(exc).__name__
                continue
            except (KeyError, ValueError, ValidationError):
                self.last_error = "invalid model response"
                continue
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        self.total_latency_ms += self.last_latency_ms
        if self.inference_status not in {"rate_limited", "degraded"}:
            self.inference_status = "error"
        return fallback

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
