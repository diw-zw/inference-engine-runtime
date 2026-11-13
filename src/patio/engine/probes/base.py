from abc import ABC, abstractmethod

from patio.registration.models import QuiescentState


class EngineProbe(ABC):
    @abstractmethod
    def ready(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def quiescent(self) -> QuiescentState:
        raise NotImplementedError
