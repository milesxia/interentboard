from __future__ import annotations

import asyncio
import json

import httpx

from app.config import choose_model


class OllamaClient:
    def __init__(self, base_url: str, requested_model: str, num_ctx: int = 8192, timeout: int = 900, mock: bool = False):
        self.base_url = base_url.rstrip("/")
        self.model = choose_model(requested_model)
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.mock = mock
        self._model_lock = asyncio.Lock()

    async def health(self) -> dict:
        if self.mock:
            return {"ok": True, "mock": True, "model": self.model, "models": [self.model]}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                models = [m.get("name") for m in r.json().get("models", [])]
                return {"ok": True, "model": self.model, "models": models}
        except Exception as e:
            return {"ok": False, "model": self.model, "error": str(e), "models": []}

    def _is_present(self, models: list[str]) -> bool:
        requested = self.model.split(":latest")[0]
        return any((m or "").split(":latest")[0] == requested for m in models)

    async def ensure_model(self) -> dict:
        if self.mock:
            return {"ok": True, "model": self.model, "pulled": False, "mock": True}
        async with self._model_lock:
            h = await self.health()
            if not h.get("ok"):
                return h
            if self._is_present(h.get("models", [])):
                return {"ok": True, "model": self.model, "pulled": False}
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    r = await client.post(f"{self.base_url}/api/pull", json={"model": self.model, "stream": False})
                    r.raise_for_status()
                    return {"ok": True, "model": self.model, "pulled": True}
            except Exception as e:
                return {"ok": False, "model": self.model, "error": str(e)}

    async def chat_json(self, system: str, user: str) -> dict:
        if self.mock:
            return {
                "current_state": "测试模式下当前状态保持不变。",
                "conclusion": "本次为测试模式：已完成候选信息抓取、去重和规则分析。",
                "changes": [],
                "risk_direction": "unchanged",
                "stage_changes": [],
                "predictions": [],
                "next_watch": [],
                "watch_updates": [],
                "confidence": 0.99,
            }
        ready = await self.ensure_model()
        if not ready.get("ok"):
            raise RuntimeError(f"本地模型不可用: {ready.get('error', 'unknown error')}")
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": 0.1,
                "num_ctx": self.num_ctx,
                "num_predict": 1800,
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            content = r.json().get("message", {}).get("content", "{}")
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                start, end = content.find("{"), content.rfind("}")
                if start >= 0 and end > start:
                    return json.loads(content[start : end + 1])
                raise
