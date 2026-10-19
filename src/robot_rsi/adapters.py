"""One installed factory selected once by the host. No hot loading, scanning loop or SDK owner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import entry_points

from .capabilities import SkillBindings
from .contracts import Contract, HarnessError
from .resources import CalibrationRecord, Resource, RobotModel


@dataclass(frozen=True)
class AdapterContext:
    profile: object
    store: object
    options: Contract
    depth_predictor: object = None
    robot_transport: object = None
    bindings_factory: object = None


@dataclass
class Adapter:
    bindings: SkillBindings
    resources: tuple[Resource, ...]
    owner: object = None
    lifecycles: dict = field(default_factory=dict)
    models: tuple[RobotModel, ...] = ()
    calibrations: tuple[CalibrationRecord, ...] = ()
    depth_provider: object = None
    candidates: tuple = ()
    connected: set = field(default_factory=set, init=False)
    closed: bool = field(default=False, init=False)
    _lifecycle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False, compare=False
    )

    def describe(self):
        return [
            {
                **r.model_dump(),
                "connected": id(self.lifecycles.get(r.owner_id)) in self.connected
                if r.owner_id
                else None,
                "connection_source": "host_lifecycle; not a live probe",
            }
            for r in self.resources
        ]

    async def connect(self, resource_id):
        # Hold through I/O so owner aliases and shutdown share the same boundary.
        async with self._lifecycle_lock:
            if self.closed:
                raise HarnessError("runtime_closed", "Adapter lifecycle is closed")
            resource = next((r for r in self.resources if r.resource_id == resource_id), None)
            if resource is None:
                raise HarnessError("not_found", "Resource not configured")
            owner = self.lifecycles.get(resource.owner_id)
            if owner is not None and id(owner) not in self.connected:
                await owner.connect()
                self.connected.add(id(owner))
            return next(r for r in self.describe() if r["resource_id"] == resource_id)

    async def close(self):
        async with self._lifecycle_lock:
            if self.closed:
                return
            self.closed = True
            seen = set()
            errors = []
            try:
                for owner in reversed(list(self.lifecycles.values())):
                    if id(owner) not in seen:
                        seen.add(id(owner))
                        try:
                            await owner.close()
                        except Exception as exc:
                            errors.append(exc)
            finally:
                self.connected.clear()
            if errors:
                raise errors[0]


@lru_cache(maxsize=1)
def factories():
    entries = {}
    for entry in entry_points(group="robot_rsi.adapters"):
        if entry.name in entries:
            raise HarnessError("ambiguous_adapter", "Duplicate adapter entry point")
        entries[entry.name] = entry
    return entries


def factory_config(profile):
    name = profile.host.adapter.factory if profile.host.adapter else profile.robot
    entry = factories().get(name)
    if entry is None:
        raise HarnessError("adapter_unavailable", "Install the selected robot_rsi.adapters package")
    factory = entry.load()
    options = factory.config_model.model_validate(
        profile.host.adapter.options if profile.host.adapter else {}
    )
    return name, factory, options


def config_check(profile):
    name, _, _ = factory_config(profile)
    return {
        "valid": True,
        "schema_version": profile.host.schema_version,
        "adapter": name,
        "inputs_and_hardware_checked": False,
        "connected": False,
    }


def build_adapter(profile, store, **overrides):
    _, factory, options = factory_config(profile)
    result = factory(AdapterContext(profile, store, options, **overrides))
    if not isinstance(result, Adapter):
        raise HarnessError("invalid_adapter", "Factory must return Adapter")
    names = [r.resource_id for r in result.resources]
    if len(set(names)) != len(names):
        raise HarnessError("invalid_adapter", "Duplicate resource IDs")
    for resource in result.resources:
        if resource.owner_id and resource.owner_id not in result.lifecycles:
            raise HarnessError("invalid_adapter", "Resource owner lifecycle is missing")
    result.bindings.catalog()
    if result.owner is None and not result.bindings.missing({"robot.command": 1}):
        raise HarnessError("invalid_adapter", "Device commands require one execution owner")
    return result
