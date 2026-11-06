"""Bounded asynchronous work without creating a task for every input row."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


async def run_bounded(items: Sequence[T], operation: Callable[[T], Awaitable[None]],
                      *, limit: int = 5) -> None:
    if limit < 1:
        raise ValueError("Concurrency limit must be positive")
    iterator = iter(items)

    async def worker() -> None:
        for item in iterator:
            await operation(item)

    # A failed/cancelled caller must not leave workers editing users behind it.
    async with asyncio.TaskGroup() as group:
        for _ in range(min(limit, len(items))):
            group.create_task(worker())
