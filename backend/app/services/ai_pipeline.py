import asyncio
import time
import json
import httpx
from typing import Dict, Any, Optional
from collections import deque
import uuid
from app.core.rate_limiter import rate_limiter
from app.core.config import settings

class AIRequest:
    def __init__(self, workflow: str, prompt: str, priority: int = 5, model: str = None, extra: Dict[str,Any]=None):
        self.id = str(uuid.uuid4())[:8]
        self.workflow = workflow  # e.g., resume_parse, scoring, resume_gen, tagging, classify, email_gen, form_detect
        self.prompt = prompt
        self.priority = priority  # 1 highest
        self.model = model
        self.extra = extra or {}
        self.status = "queued"
        self.created_at = time.time()
        self.result = None
        self.error = None

class AIPipeline:
    def __init__(self):
        self.queue: deque = deque()
        self.processing: Optional[AIRequest] = None
        self.history: deque = deque(maxlen=100)
        self.lock = asyncio.Lock()
        self.running = False
        # priority mapping
        self.workflow_priority = {
            "application_autofill": 1,
            "resume_gen": 2,
            "scoring": 3,
            "tagging": 4,
            "classify": 5,
            "email_gen": 5,
            "form_detect": 3,
            "parse": 2,
            "keyword_extract": 3,
            "funding_scan": 3,
        }

    async def enqueue(self, workflow: str, prompt: str, priority: int = None, model: str = None, extra: Dict[str,Any]=None) -> AIRequest:
        if priority is None:
            priority = self.workflow_priority.get(workflow, 5)
        req = AIRequest(workflow, prompt, priority, model, extra)
        async with self.lock:
            # Insert by priority (lower number first) - simple insertion sort
            inserted = False
            for idx, existing in enumerate(self.queue):
                if req.priority < existing.priority:
                    self.queue.insert(idx, req)
                    inserted = True
                    break
            if not inserted:
                self.queue.append(req)
        return req

    async def dequeue(self) -> Optional[AIRequest]:
        async with self.lock:
            if self.queue:
                # sort again just in case
                lst = list(self.queue)
                lst.sort(key=lambda r: (r.priority, r.created_at))
                self.queue = deque(lst)
                req = self.queue.popleft()
                self.processing = req
                req.status = "processing"
                return req
            self.processing = None
            return None

    async def mark_done(self, req: AIRequest, result: Any = None, error: str = None):
        async with self.lock:
            req.status = "failed" if error else "done"
            req.result = result
            req.error = error
            self.history.append(req)
            if self.processing and self.processing.id == req.id:
                self.processing = None

    async def get_stats(self):
        async with self.lock:
            queued = len(self.queue)
            hist = list(self.history)
            done = sum(1 for r in hist if r.status=="done")
            failed = sum(1 for r in hist if r.status=="failed")
            processing = 1 if self.processing else 0
            return {
                "queued": queued,
                "processing": processing,
                "done": done,
                "failed": failed,
                "needs_input": 0,
                "queue_preview": [{"id": r.id, "workflow": r.workflow, "priority": r.priority, "status": r.status} for r in list(self.queue)[:10]],
                "processing_now": {"id": self.processing.id, "workflow": self.processing.workflow} if self.processing else None,
            }

    async def worker_loop(self):
        self.running = True
        while self.running:
            req = await self.dequeue()
            if not req:
                await asyncio.sleep(0.5)
                continue
            # Respect rate limiter
            await rate_limiter.wait_and_acquire(1)
            # Simulate / actually call AI if configured
            try:
                # If prompt is JSON-like, we treat as generic completion
                # For demo, mock result after short delay
                await asyncio.sleep(0.8)  # simulate latency
                # If AI configured, try real call for workflows that need it
                # Here we just mock
                result = {"mock": True, "workflow": req.workflow, "prompt_preview": req.prompt[:200]}
                await self.mark_done(req, result=result)
            except Exception as e:
                await self.mark_done(req, error=str(e))
            await asyncio.sleep(0.1)

    async def health_check(self) -> Dict[str,Any]:
        if not settings.ai_api_key:
            return {"online": False, "reason": "no_api_key"}
        # Try ping
        base_url = settings.ai_base_url
        headers = {"Authorization": f"Bearer {settings.ai_api_key}"}
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
                latency = int((time.time()-start)*1000)
                if resp.status_code in (200, 401, 403):  # 401 means reachable but bad key -> consider online=False? We'll be permissive
                    online = resp.status_code==200
                    return {"online": online, "latency_ms": latency, "status": resp.status_code}
                return {"online": False, "latency_ms": latency, "status": resp.status_code}
        except Exception as e:
            return {"online": False, "error": str(e), "latency_ms": None}

ai_pipeline = AIPipeline()
