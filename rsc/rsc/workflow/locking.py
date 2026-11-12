"""Task ownership locks shared by command and service entry points."""

import fcntl
from contextlib import contextmanager

from .store import StoreError as WorkflowError


@contextmanager
def exclusive(path):
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkflowError(f"task component is already running: {path.name}") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
