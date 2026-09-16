class IpswExtractError(Exception):
    pass


class IpswExtractTimeoutError(IpswExtractError, TimeoutError):
    pass


class IpswMountCleanupError(IpswExtractError):
    """Detach could not be confirmed. Retain all inputs and abort the worker.

    Callers must not recursively clean processing directories while handling
    this error. A preceding extraction/cancellation error is retained as cause.
    """
