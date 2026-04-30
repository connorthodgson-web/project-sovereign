"""Registry for executable local and managed agent adapters."""

from __future__ import annotations

from collections import OrderedDict

from agents.adapter import AgentAdapter


class AgentRegistry:
    """Lookup and capability selection for agent adapters."""

    def __init__(self) -> None:
        self._adapters: OrderedDict[str, AgentAdapter] = OrderedDict()
        self._aliases: dict[str, str] = {}

    def register(self, adapter: AgentAdapter) -> AgentAdapter:
        self._adapters[adapter.agent_id] = adapter
        self._aliases[adapter.agent_id] = adapter.agent_id
        for alias in adapter.aliases:
            self._aliases[alias] = adapter.agent_id
        return adapter

    def get(self, agent_id: str) -> AgentAdapter | None:
        resolved = self._aliases.get(agent_id, agent_id)
        return self._adapters.get(resolved)

    def descriptor_for(self, agent_id: str) -> dict[str, object] | None:
        adapter = self.get(agent_id)
        if adapter is None:
            return None
        return adapter.descriptor.model_dump()

    def list_agents(self, *, include_disabled: bool = True) -> list[AgentAdapter]:
        adapters = list(self._adapters.values())
        if include_disabled:
            return adapters
        return [adapter for adapter in adapters if adapter.enabled]

    def list_descriptors(self, *, include_disabled: bool = True) -> list[dict[str, object]]:
        return [
            adapter.descriptor.model_dump()
            for adapter in self.list_agents(include_disabled=include_disabled)
        ]

    def candidates_for_capability(
        self,
        capability: str,
        *,
        include_disabled: bool = False,
    ) -> list[AgentAdapter]:
        candidates = [
            adapter
            for adapter in self._adapters.values()
            if adapter.supports_capability(capability)
            and (include_disabled or adapter.enabled)
        ]
        return candidates

    def capability_snapshot(self, *, include_disabled: bool = False) -> dict[str, list[str]]:
        snapshot: dict[str, list[str]] = {}
        for adapter in self.list_agents(include_disabled=include_disabled):
            for capability in adapter.descriptor.capabilities:
                snapshot.setdefault(capability, []).append(adapter.agent_id)
        return snapshot
