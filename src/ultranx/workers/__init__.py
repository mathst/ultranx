"""Workers QThread: fronteira entre o domínio e o event loop da UI."""

from .update_worker import UpdateWorker
from .version_worker import VersionWorker

__all__ = ["UpdateWorker", "VersionWorker"]
