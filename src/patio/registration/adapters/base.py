from abc import ABC, abstractmethod

from patio.registration.models import MembershipState


class RouterAdapter(ABC):
    @property
    @abstractmethod
    def protocol(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def add(self) -> MembershipState:
        raise NotImplementedError

    @abstractmethod
    def remove(self) -> MembershipState:
        raise NotImplementedError

    @abstractmethod
    def query(self) -> MembershipState:
        raise NotImplementedError
