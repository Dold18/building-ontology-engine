"""Stable operation errors; safe local retries never replay externally visible requests."""
from dataclasses import dataclass
import random
import time

@dataclass
class EngineError(ValueError):
    code: str
    message: str
    retryable: bool = False
    next_action: str = 'Inspect restricted run state; correct cause before explicit resume.'
    attempt_count: int = 0

    def __str__(self):
        return self.code + ': ' + self.message

    def as_dict(self):
        return {'error_code': self.code, 'message': self.message, 'retryable': self.retryable,
                'next_action': self.next_action, 'attempt_count': self.attempt_count}


def classify(exc):
    if isinstance(exc, EngineError): return exc
    if isinstance(getattr(exc, "__cause__", None), EngineError): return exc.__cause__
    code = 'INVALID_INPUT' if isinstance(exc, ValueError) else 'LOCAL_IO_ERROR' if isinstance(exc, OSError) else 'PROGRAMMING_DEFECT'
    return EngineError(code, 'Operation failed; original evidence retained in restricted storage.', code == 'LOCAL_IO_ERROR')


def retry_local(operation, attempts=3, observer=None):
    if not 1 <= attempts <= 3: raise ValueError('Local retry budget must be between one and three')
    for attempt in range(1, attempts + 1):
        try:
            result = operation()
            if observer: observer(attempt, 'COMPLETED')
            return result
        except OSError:
            if observer: observer(attempt, 'LOCAL_IO_ERROR')
            if attempt == attempts:
                raise EngineError('LOCAL_RETRY_EXHAUSTED', 'Idempotent local operation exhausted its retry budget.',
                                  False, attempt_count=attempt) from None
            time.sleep(min(.01 * 2 ** (attempt - 1) + random.random() * .005, .05))
