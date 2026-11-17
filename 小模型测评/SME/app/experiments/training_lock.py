"""Serialize training that seeds process-global RNGs, including nested adapters."""
from functools import wraps
from threading import RLock

TRAINING_LOCK = RLock()


def training_locked(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with TRAINING_LOCK:
            return function(*args, **kwargs)
    return locked
