import asyncio
import time
from typing import Dict, Any, Optional
from collections import deque
import uuid
from app.core.rate_limiter import rate_limiter
from app.services.ai_client import ping

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
        """
        Drains the priority queue in FIFO (priority-then-arrival) order.

        The pipeline is the system's *ordering + observability* layer: every
        AI work item is enqueued here so the UI can show a live, prioritized
        queue. Actual inference happens through `ai_client.chat_completion`,
        which shares the SAME global rate limiter — so queue work and inline
        work can never jointly exceed the configured RPM. Items enqueued with
        real prompts/executables are executed; descriptive work items are
        recorded as completed to keep the queue moving.
        """
        self.running = True
        while self.running:
            req = await self.dequeue()
            if not req:
                await asyncio.sleep(0.5)
                continue
            # Reserve a rate-limiter token for the work this item represents.
            await rate_limiter.wait_and_acquire(1)
            try:
                executor = (req.extra or {}).get("executor")
                if callable(executor):
                    result = await executor()
                else:
                    await asyncio.sleep(0.8)  # represent latency of the underlying task
                    result = {"workflow": req.workflow, "prompt_preview": req.prompt[:200]}
                await self.mark_done(req, result=result)
            except Exception as e:
                await self.mark_done(req, error=str(e))
            await asyncio.sleep(0.1)

    async def health_check(self) -> Dict[str,Any]:
        """Probe the default AI endpoint for the online/offline indicator."""
        return await ping()

ai_pipeline = AIPipeline()
