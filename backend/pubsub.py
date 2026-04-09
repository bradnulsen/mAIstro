"""Event broadcast — pub/sub for task-level and queue-level SSE streams.

Two independent broadcast channels:
  - Task-level: per-task subscriber queues for live streaming (text, thinking,
    tool_use, etc.) to the task detail drawer.
  - Queue-level: global subscriber set for queue-change notifications. Clients
    re-fetch the task queue on each notification.

Consumers: queue_routes.py (SSE endpoints), worker.py (event producer).
"""

import asyncio
import logging

from backend import events

log = logging.getLogger("maistro.pubsub")

# ── Task-level broadcast ──────────────────────────────────
# Subscribers keyed by task_id → set of asyncio.Queue.
_subscribers: dict[int, set[asyncio.Queue]] = {}


def subscribe(task_id: int) -> asyncio.Queue:
    """Subscribe to live events for a task. Returns a queue to read from."""
    q = asyncio.Queue()
    _subscribers.setdefault(task_id, set()).add(q)
    return q


def unsubscribe(task_id: int, q: asyncio.Queue):
    """Remove a subscriber queue for a task."""
    subs = _subscribers.get(task_id)
    if subs:
        subs.discard(q)
        if not subs:
            del _subscribers[task_id]


def broadcast(task_id: int, event: dict):
    """Push an event to all subscribers of a task."""
    subs = _subscribers.get(task_id)
    if not subs:
        return
    for q in subs:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def cleanup_task(task_id: int):
    """Remove all subscribers for a task (call on task completion)."""
    _subscribers.pop(task_id, None)


# ── Queue-level broadcast ─────────────────────────────────
# Global subscribers that want to know when any task transitions state.
_queue_subscribers: set[asyncio.Queue] = set()


def subscribe_queue() -> asyncio.Queue:
    """Subscribe to global queue-change notifications. Returns a queue to read from."""
    q = asyncio.Queue()
    _queue_subscribers.add(q)
    return q


def unsubscribe_queue(q: asyncio.Queue):
    """Remove a global queue subscriber."""
    _queue_subscribers.discard(q)


def notify_queue_changed():
    """Notify all global queue subscribers that the queue state has changed."""
    for q in _queue_subscribers:
        try:
            q.put_nowait(events.queue_changed())
        except asyncio.QueueFull:
            pass
