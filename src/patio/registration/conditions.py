import threading
from abc import ABC, abstractmethod
from typing import Callable

from patio.registration.models import RoleInstanceState


class Condition(ABC):
    @abstractmethod
    def value(self) -> bool:
        raise NotImplementedError


class ProcessCondition(Condition):
    def __init__(self) -> None:
        self._stopped = threading.Event()

    def stop(self) -> None:
        self._stopped.set()

    def value(self) -> bool:
        return not self._stopped.is_set()


class ManualDrainCondition(Condition):
    _DRAIN_KEY = "patio.workloads.x-k8s.io/drain"

    def __init__(self, path: str = "/etc/patio/annotations") -> None:
        self.path = path

    def value(self) -> bool:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                content = file.read()
        except FileNotFoundError:
            return True
        except OSError:
            return False

        return not self._is_draining(content)

    def _is_draining(self, content: str) -> bool:
        for line in content.splitlines():
            if "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            value = raw_value.strip().strip('"').strip("'").lower()
            if key.strip() == self._DRAIN_KEY and value in {"true", "1", "yes"}:
                return True
        return False


class EngineCondition(Condition):
    def __init__(self, probe) -> None:
        self.probe = probe

    def value(self) -> bool:
        return self.probe.ready()


class RBGCondition(Condition):
    def __init__(
        self,
        snapshot_provider: Callable[[], RoleInstanceState | None] | None = None,
    ) -> None:
        self.snapshot_provider = snapshot_provider

    def value(self) -> bool:
        if self.snapshot_provider is None:
            return True

        snapshot = self.snapshot_provider()
        if snapshot is None:
            return False

        return (
            snapshot.phase == "Running"
            and snapshot.hook_state in {None, "", "PostReady"}
        )
