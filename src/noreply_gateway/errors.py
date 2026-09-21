class DeliveryError(Exception):
    """Messages in these exceptions are safe operator-facing error codes."""


class Retryable(DeliveryError):
    def __init__(self, message: str, delay: float = 0, global_cooldown: bool = False):
        super().__init__(message)
        self.delay = delay
        self.global_cooldown = global_cooldown


class AuthenticationRequired(DeliveryError):
    pass


class Permanent(DeliveryError):
    pass


class Uncertain(DeliveryError):
    pass
