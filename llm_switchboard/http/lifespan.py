"""One application lifespan owns the supervisor and the indexer scheduler."""

import asyncio
from contextlib import asynccontextmanager
import threading

from ..search_process import SearchIndexScheduler


@asynccontextmanager
async def service_lifespan(app):
    supervisor = app.state.domain.supervisor
    stop = threading.Event()
    worker = threading.Thread(target=supervisor.run_forever, args=(stop,), name="supervisor")
    index_worker = SearchIndexScheduler().start(stop)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        supervisor.wake()
        await asyncio.to_thread(worker.join, 10)
        await asyncio.to_thread(index_worker.join, 12)
        # The tmux server belongs to its own systemd scope, not this lifespan.
        # An ordinary service restart must leave every running session intact.
