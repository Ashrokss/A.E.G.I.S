import asyncio
import logging
from collections import defaultdict

# Global log queues for SSE streaming
log_queues = defaultdict(asyncio.Queue)
logger = logging.getLogger("runlogs")

def add_run_log(run_id: str, message: str):
    # 1) Push to SSE queue for UI live logs.
    try:
        log_queues[run_id].put_nowait(message)
    except Exception:
        pass

    # 2) Mirror to container stdout so docker compose shows it too.
    logger.info("[run:%s] %s", run_id, str(message).rstrip())
