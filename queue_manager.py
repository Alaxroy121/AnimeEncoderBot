"""
Async task queue manager for AnimeEncoderBot.
Handles priority queuing, concurrency limits, retries, and timeouts.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from config import Config
from database import db

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class TaskType(str, Enum):
    ENCODE = "encode"
    UPSCALE = "upscale"


@dataclass
class Task:
    """Represents a queued task."""
    task_id: str
    user_id: int
    task_type: TaskType
    input_file: str
    settings: dict
    priority: int = 0          # Higher = processed first (admins get +10)
    max_retries: int = 2
    retries: int = 0
    status: TaskStatus = TaskStatus.QUEUED
    progress_message_id: Optional[int] = None
    progress_chat_id: Optional[int] = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    @staticmethod
    def generate_id() -> str:
        """Generate a unique task ID."""
        return f"task_{uuid.uuid4().hex[:12]}"


class QueueManager:
    """Manages the encoding/upscaling task queue."""

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._task_registry: dict[str, Task] = {}
        self._workers: list[asyncio.Task] = []
        self._running: bool = False
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._process_fn: Optional[Callable[..., Coroutine]] = None

    def set_processor(self, fn: Callable[..., Coroutine]) -> None:
        """Set the async function that processes tasks.

        The function should accept (task: Task) and handle encoding/upscaling.
        """
        self._process_fn = fn

    async def start(self, num_workers: Optional[int] = None) -> None:
        """Start queue workers."""
        if self._running:
            return

        workers = num_workers or Config.CONCURRENT_TASKS
        self._semaphore = asyncio.Semaphore(workers)
        self._running = True

        # Start worker tasks
        for i in range(workers):
            worker = asyncio.create_task(self._worker(i), name=f"queue_worker_{i}")
            self._workers.append(worker)

        # Restore queued tasks from DB
        await self._restore_queued_tasks()

        logger.info("Queue manager started with %d workers", workers)

    async def stop(self) -> None:
        """Stop all workers gracefully."""
        self._running = False

        # Cancel all worker tasks
        for worker in self._workers:
            worker.cancel()

        # Wait for workers to finish
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

        self._workers.clear()
        logger.info("Queue manager stopped")

    async def add_task(self, task: Task) -> str:
        """Add a task to the queue. Returns task_id."""
        # Register in DB
        await db.create_task({
            "task_id": task.task_id,
            "user_id": task.user_id,
            "type": task.task_type.value,
            "status": task.status.value,
            "input_file": task.input_file,
            "settings": task.settings,
            "priority": task.priority,
            "output_file": None,
        })

        # Add to in-memory registry and queue
        self._task_registry[task.task_id] = task
        # Priority queue: negate priority so higher values come first
        await self._queue.put((-task.priority, time.time(), task.task_id))

        position = await db.get_queue_position(task.task_id)
        logger.info(
            "Task %s queued (user: %d, type: %s, priority: %d, position: %d)",
            task.task_id, task.user_id, task.task_type.value, task.priority, position,
        )
        return task.task_id

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a task. Returns True if cancelled."""
        task = self._task_registry.get(task_id)
        if not task:
            return await db.cancel_task(task_id)

        # Signal cancellation
        task.cancel_event.set()
        task.status = TaskStatus.CANCELLED

        # Cancel running asyncio task if active
        if task_id in self._active_tasks:
            self._active_tasks[task_id].cancel()

        await db.update_task(task_id, {"status": TaskStatus.CANCELLED.value})
        logger.info("Task %s cancelled", task_id)
        return True

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task from the registry."""
        return self._task_registry.get(task_id)

    async def get_queue_info(self) -> dict:
        """Get queue status information."""
        processing_count = len(self._active_tasks)
        queued = await db.get_queued_tasks(limit=100)
        return {
            "queued": len(queued),
            "processing": processing_count,
            "total_workers": len(self._workers),
            "tasks": queued[:20],  # First 20 for display
        }

    async def _worker(self, worker_id: int) -> None:
        """Worker coroutine that processes tasks from the queue."""
        logger.debug("Worker %d started", worker_id)

        while self._running:
            try:
                # Get next task from queue (blocks until available)
                try:
                    _, _, task_id = await asyncio.wait_for(
                        self._queue.get(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                task = self._task_registry.get(task_id)
                if not task or task.status == TaskStatus.CANCELLED:
                    continue

                # Process the task
                async with self._semaphore:
                    await self._process_task(task, worker_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Worker %d error: %s", worker_id, e, exc_info=True)
                await asyncio.sleep(1)

        logger.debug("Worker %d stopped", worker_id)

    async def _process_task(self, task: Task, worker_id: int) -> None:
        """Process a single task with timeout and retry logic."""
        task.status = TaskStatus.PROCESSING
        await db.update_task(task.task_id, {
            "status": TaskStatus.PROCESSING.value,
            "started_at": None,  # Will be set by $currentDate in a real impl
        })

        logger.info("Worker %d processing task %s", worker_id, task.task_id)

        try:
            # Create a tracked asyncio task
            coro = self._process_fn(task) if self._process_fn else asyncio.sleep(0)
            atask = asyncio.create_task(coro)
            self._active_tasks[task.task_id] = atask

            # Run with timeout
            await asyncio.wait_for(atask, timeout=Config.TASK_TIMEOUT)

            # Success
            task.status = TaskStatus.COMPLETED
            await db.update_task(task.task_id, {"status": TaskStatus.COMPLETED.value})
            await db.increment_user_tasks(task.user_id)

            # Update global stats
            input_size = 0
            try:
                import os
                input_size = os.path.getsize(task.input_file)
            except Exception:
                pass
            await db.increment_stats(task.task_type.value, input_size)

            logger.info("Task %s completed successfully", task.task_id)

        except asyncio.TimeoutError:
            logger.warning("Task %s timed out", task.task_id)
            task.status = TaskStatus.TIMEOUT
            await db.update_task(task.task_id, {
                "status": TaskStatus.TIMEOUT.value,
                "error": f"Task timed out after {Config.TASK_TIMEOUT}s",
            })

        except asyncio.CancelledError:
            logger.info("Task %s was cancelled", task.task_id)
            task.status = TaskStatus.CANCELLED
            await db.update_task(task.task_id, {"status": TaskStatus.CANCELLED.value})

        except Exception as e:
            logger.error("Task %s failed: %s", task.task_id, e, exc_info=True)

            # Retry logic
            if task.retries < task.max_retries:
                task.retries += 1
                task.status = TaskStatus.QUEUED
                await db.update_task(task.task_id, {
                    "status": TaskStatus.QUEUED.value,
                    "error": f"Retry {task.retries}/{task.max_retries}: {str(e)}",
                })
                await self._queue.put((-task.priority, time.time(), task.task_id))
                logger.info("Task %s queued for retry (%d/%d)", task.task_id, task.retries, task.max_retries)
            else:
                task.status = TaskStatus.FAILED
                await db.update_task(task.task_id, {
                    "status": TaskStatus.FAILED.value,
                    "error": str(e),
                })

        finally:
            self._active_tasks.pop(task.task_id, None)

    async def _restore_queued_tasks(self) -> None:
        """Restore previously queued tasks from DB on startup."""
        queued = await db.get_queued_tasks()
        restored = 0
        for doc in queued:
            task = Task(
                task_id=doc["task_id"],
                user_id=doc["user_id"],
                task_type=TaskType(doc["type"]),
                input_file=doc.get("input_file", ""),
                settings=doc.get("settings", {}),
                priority=doc.get("priority", 0),
            )
            self._task_registry[task.task_id] = task
            await self._queue.put((-task.priority, time.time(), task.task_id))
            restored += 1

        if restored:
            logger.info("Restored %d queued tasks from database", restored)


# Global queue manager instance
queue_manager = QueueManager()
