from __future__ import annotations

import asyncio
import json
import hashlib
import math
import time
from typing import Any

import httpx

from app.config import choose_model


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        requested_model: str,
        extract_model: str = "qwen3:4b-instruct-2507-q4_K_M",
        num_ctx: int = 8192,
        num_gpu: int = 4,
        extract_num_gpu: int = 99,
        num_thread: int = 6,
        extract_num_thread: int = 4,
        final_think: str = "medium",
        keep_alive: str = "10m",
        timeout: int = 1800,
        mock: bool = False,
        embed_model: str = "qwen3-embedding:0.6b",
        metrics_sink=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = choose_model(requested_model)
        self.extract_model = extract_model or "qwen3:4b-instruct-2507-q4_K_M"
        self.num_ctx = num_ctx
        self.num_gpu = num_gpu
        self.extract_num_gpu = extract_num_gpu
        self.num_thread = num_thread
        self.extract_num_thread = extract_num_thread
        self.final_think = final_think
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.mock = mock
        self.embed_model = embed_model or "qwen3-embedding:0.6b"
        self.metrics_sink = metrics_sink
        self._model_lock = asyncio.Lock()
        self.last_metrics: dict[str, dict[str, Any]] = {}
        self.last_gpu_layers: dict[str, int] = {}

    async def health(self) -> dict:
        if self.mock:
            return {
                "ok": True,
                "mock": True,
                "model": self.model,
                "extract_model": self.extract_model,
                "models": [self.model, self.extract_model],
                "running": [],
                "metrics": self.last_metrics,
            }
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                tags = await client.get(f"{self.base_url}/api/tags")
                tags.raise_for_status()
                models = [m.get("name") for m in tags.json().get("models", [])]
                running = []
                try:
                    ps = await client.get(f"{self.base_url}/api/ps")
                    if ps.is_success:
                        running = ps.json().get("models", [])
                except Exception:
                    pass
                return {
                    "ok": True,
                    "model": self.model,
                    "extract_model": self.extract_model,
                    "embed_model": self.embed_model,
                    "models": models,
                    "running": running,
                    "metrics": self.last_metrics,
                    "gpu_layers": self.last_gpu_layers,
                }
        except Exception as e:
            return {
                "ok": False,
                "model": self.model,
                "extract_model": self.extract_model,
                "embed_model": self.embed_model,
                "error": str(e),
                "models": [],
                "running": [],
            }

    @staticmethod
    def _is_present(model: str, models: list[str]) -> bool:
        requested = model.split(":latest")[0]
        return any((m or "").split(":latest")[0] == requested for m in models)

    async def ensure_model(self, model: str) -> dict:
        if self.mock:
            return {"ok": True, "model": model, "pulled": False, "mock": True}
        async with self._model_lock:
            h = await self.health()
            if not h.get("ok"):
                return h
            if self._is_present(model, h.get("models", [])):
                return {"ok": True, "model": model, "pulled": False}
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    # Stream so the Ollama connection itself can continue for large model downloads.
                    async with client.stream("POST", f"{self.base_url}/api/pull", json={"model": model, "stream": True}) as r:
                        r.raise_for_status()
                        async for _ in r.aiter_lines():
                            pass
                return {"ok": True, "model": model, "pulled": True}
            except Exception as e:
                return {"ok": False, "model": model, "error": str(e)}

    async def ensure_required_models(self) -> dict:
        out = []
        for model in dict.fromkeys([self.extract_model, self.model]):
            out.append(await self.ensure_model(model))
        return {"ok": all(x.get("ok") for x in out), "models": out}

    async def unload(self, model: str) -> None:
        if self.mock:
            return
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
                )
        except Exception:
            pass

    @staticmethod
    def _gpu_candidates(requested: int, extraction: bool) -> list[int]:
        if requested <= 0:
            return [0]
        if extraction and requested >= 32:
            return [requested, 48, 40, 32, 28, 24, 20, 16, 12, 8, 4]
        return list(dict.fromkeys([requested, max(1, requested - 1), max(1, requested - 2), 1]))

    @staticmethod
    def _should_retry_gpu(exc: Exception) -> bool:
        text = str(exc).lower()
        markers = ("cuda", "out of memory", "memory allocation", "vram", "ggml", "runner process")
        return any(x in text for x in markers)

    def _mock_response(self, purpose: str) -> dict:
        if purpose == "extract":
            return {
                "claims": [
                    {
                        "statement": "测试模式：已完整处理当前分块。",
                        "type": "fact",
                        "event_date": None,
                        "certainty": "confirmed",
                        "confidence": 0.99,
                        "entities": [],
                        "tags": ["mock"],
                    }
                ],
                "chunk_summary": "测试模式分块摘要。",
                "has_material_change": False,
            }
        if purpose == "reduce":
            return {"claims": [], "summary": "测试模式批次融合。", "conflicts": []}
        return {
            "current_state": "测试模式下当前状态保持不变。",
            "conclusion": "本次为测试模式：分块、断点账本、知识检索与综合分析流程均已完成。",
            "changes": [],
            "risk_direction": "unchanged",
            "stage_changes": [],
            "predictions": [],
            "next_watch": [],
            "watch_updates": [],
            "confidence": 0.99,
        }

    async def embed_texts(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        model = model or self.embed_model
        texts = [str(x or "") for x in texts]
        if not texts:
            return []
        if self.mock:
            out = []
            for text in texts:
                raw = hashlib.sha256(text.encode("utf-8", "ignore")).digest()
                vec = [((raw[i % len(raw)] / 255.0) * 2.0 - 1.0) for i in range(64)]
                norm = math.sqrt(sum(x*x for x in vec)) or 1.0
                out.append([x / norm for x in vec])
            return out
        ready = await self.ensure_model(model)
        if not ready.get("ok"):
            raise RuntimeError(f"本地向量模型不可用 {model}: {ready.get('error', 'unknown error')}")
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{self.base_url}/api/embed", json={"model": model, "input": texts})
                r.raise_for_status()
                body = r.json()
            vectors = body.get("embeddings") or []
            if len(vectors) != len(texts):
                raise ValueError("embedding response count mismatch")
            if self.metrics_sink:
                self.metrics_sink({"purpose":"embed","model":model,"prompt_tokens":int(body.get("prompt_eval_count") or 0),"eval_tokens":0,"duration_ms":int((time.perf_counter()-start)*1000),"success":True})
            return vectors
        except Exception as exc:
            if self.metrics_sink:
                self.metrics_sink({"purpose":"embed","model":model,"duration_ms":int((time.perf_counter()-start)*1000),"success":False,"error":str(exc)})
            raise

    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        purpose: str = "final",
        model: str | None = None,
        num_ctx: int | None = None,
        num_predict: int = 1000,
        temperature: float = 0.1,
        think: bool | str | None = None,
        num_gpu: int | None = None,
        num_thread: int | None = None,
    ) -> dict:
        if self.mock:
            return self._mock_response(purpose)

        extraction = purpose in {"extract", "reduce"}
        model = model or (self.extract_model if extraction else self.model)
        requested_gpu = num_gpu if num_gpu is not None else (self.extract_num_gpu if extraction else self.num_gpu)
        threads = num_thread if num_thread is not None else (self.extract_num_thread if extraction else self.num_thread)
        ctx = num_ctx or self.num_ctx
        if think is None:
            think = False if extraction else self.final_think
        if isinstance(think, str) and think in {"false", "off", "0", "none"}:
            think = False
        elif isinstance(think, str) and think in {"true", "on", "1"}:
            think = True

        ready = await self.ensure_model(model)
        if not ready.get("ok"):
            raise RuntimeError(f"本地模型不可用 {model}: {ready.get('error', 'unknown error')}")

        candidates = self._gpu_candidates(requested_gpu, extraction)
        last_exc: Exception | None = None
        request_started = time.perf_counter()
        for candidate in candidates:
            payload = {
                "model": model,
                "stream": False,
                "format": "json",
                "truncate": False,
                "keep_alive": self.keep_alive,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": {
                    "temperature": temperature,
                    "num_ctx": ctx,
                    "num_predict": num_predict,
                    "num_gpu": candidate,
                    "num_thread": threads,
                },
            }
            if think is not None:
                payload["think"] = think
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    r = await client.post(f"{self.base_url}/api/chat", json=payload)
                    if not r.is_success:
                        detail = r.text[:2000]
                        raise RuntimeError(f"Ollama HTTP {r.status_code}: {detail}")
                    body = r.json()
                self.last_gpu_layers[model] = candidate
                prompt_count = int(body.get("prompt_eval_count") or 0)
                prompt_ns = int(body.get("prompt_eval_duration") or 0)
                eval_count = int(body.get("eval_count") or 0)
                eval_ns = int(body.get("eval_duration") or 0)
                self.last_metrics[model] = {
                    "purpose": purpose,
                    "prompt_tokens": prompt_count,
                    "prompt_tps": round(prompt_count / (prompt_ns / 1e9), 2) if prompt_ns else None,
                    "eval_tokens": eval_count,
                    "eval_tps": round(eval_count / (eval_ns / 1e9), 2) if eval_ns else None,
                    "num_gpu": candidate,
                    "num_ctx": ctx,
                }
                if self.metrics_sink:
                    self.metrics_sink({**self.last_metrics[model], "model": model, "duration_ms": int((time.perf_counter()-request_started)*1000), "success": True})
                content = body.get("message", {}).get("content", "{}")
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    start, end = content.find("{"), content.rfind("}")
                    if start >= 0 and end > start:
                        parsed = json.loads(content[start : end + 1])
                    else:
                        raise ValueError(f"模型没有返回合法JSON: {content[:500]}")
                if not isinstance(parsed, dict):
                    raise ValueError("模型JSON返回值不是对象")
                return parsed
            except Exception as exc:
                last_exc = exc
                if candidate == candidates[-1] or not self._should_retry_gpu(exc):
                    if self.metrics_sink:
                        self.metrics_sink({"purpose": purpose, "model": model, "num_gpu": candidate, "num_ctx": ctx, "duration_ms": int((time.perf_counter()-request_started)*1000), "success": False, "error": str(exc)})
                    raise
                # A too-aggressive offload must never kill the job: step down and retry.
                continue
        raise last_exc or RuntimeError("Ollama request failed")
