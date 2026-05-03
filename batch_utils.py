from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, Iterable, TypeVar


T = TypeVar("T")
R = TypeVar("R")


async def run_blocking(func: Callable[..., R], *args, **kwargs) -> R:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


async def run_blocking_batch(
    items: Iterable[T],
    worker: Callable[[T], R],
    max_workers: int = 8,
    submit_delay_seconds: float = 0,
    return_exceptions: bool = False,
) -> list[R | BaseException]:
    item_list = list(items)
    if not item_list:
        return []

    worker_count = max(1, min(len(item_list), max_workers))
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures: list[asyncio.Future[R]] = []
        for index, item in enumerate(item_list):
            if index > 0 and submit_delay_seconds > 0:
                await asyncio.sleep(submit_delay_seconds)
            futures.append(loop.run_in_executor(pool, worker, item))
        return list(await asyncio.gather(*futures, return_exceptions=return_exceptions))
