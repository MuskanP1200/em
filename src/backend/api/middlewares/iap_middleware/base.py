import logging
from abc import ABC

from .authentication_backend import IapAuthenticationBackend

logger = logging.getLogger(__name__)


# Middlewares use a backend
class IAPAuthenticationMiddleware(ABC):
    def __init__(self, backend: IapAuthenticationBackend) -> None:
        self.backend = backend


class SyncIAPAuthenticationMiddleware(IAPAuthenticationMiddleware, ABC):
    def __init__(self, backend: IapAuthenticationBackend):
        super().__init__(backend)
