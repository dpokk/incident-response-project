"""Model adapter: OpenAI-compatible chat completions with tool calling (NVIDIA NIM by default).

The agent depends only on `ChatModel.chat()`; another provider (e.g. Claude) can be added as another adapter without
touching the agent. The API key is read from the environment and never logged.
"""
import os
import time

import requests

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"      # verified to support native tool calls on the NIM endpoint


class LLMError(RuntimeError):
    pass


class ChatModel:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 90):
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout

    @classmethod
    def from_env(cls) -> "ChatModel | None":
        key = os.getenv("NVIDIA_API_KEY", "").strip()
        if not key:
            return None
        return cls(os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"), key,
                   os.getenv("DEMO_AGENT_MODEL", DEFAULT_MODEL), float(os.getenv("DEMO_AGENT_TIMEOUT_S", "90")))

    @property
    def name(self) -> str:
        return f"{self.model} (NVIDIA NIM)"

    def chat(self, messages: list[dict], tools: list[dict], tool_choice="auto", max_tokens: int = 1500) -> dict:
        """One model turn. Returns {"message": {...}, "usage": {...}, "latency_s": float}."""
        body = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": tool_choice,
                "max_tokens": max_tokens, "temperature": 0.2}
        t0 = time.time()
        try:
            r = requests.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.api_key}"})
        except requests.RequestException as exc:
            raise LLMError(f"model request failed: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise LLMError(f"model returned HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError("model returned no message") from None
        return {"message": msg, "usage": data.get("usage") or {}, "latency_s": round(time.time() - t0, 2)}
