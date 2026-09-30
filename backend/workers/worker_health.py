"""Worker health monitoring and stale detection."""

from typing import List, Dict, Any
from backend.workers.worker import Worker
from backend.domain.enums import WorkerStatus, EventCode, EventLevel
from backend.events.logger import get_logger

logger = get_logger("worker_health")


class WorkerHealthMonitor:
    """Checks worker liveness via heartbeat staleness and browser session health."""

    @staticmethod
    def check_worker(worker: Worker) -> Dict[str, Any]:
        """Return health status dict for a single worker."""
        is_stale = worker.is_stale()
        session_alive = worker.session.is_alive() if worker.session else False

        healthy = (
            worker.status in (WorkerStatus.IDLE, WorkerStatus.BUSY)
            and not is_stale
            and (session_alive or worker.session is None)
        )

        return {
            "worker_id": worker.worker_id,
            "status": worker.status.value,
            "healthy": healthy,
            "is_stale": is_stale,
            "session_alive": session_alive,
            "last_heartbeat": worker.last_heartbeat,
            "current_task_id": worker.current_task_id,
        }

    @staticmethod
    def find_stale_workers(workers: List[Worker]) -> List[Worker]:
        """Return workers whose heartbeat has exceeded the stale timeout."""
        return [w for w in workers if w.is_stale() and w.status not in (WorkerStatus.STOPPED, WorkerStatus.CRASHED)]
