"""Focused coverage for the staged Zep-backed memory integration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
from core.conversation import ConversationalHandler
from core.models import AssistantDecision, ExecutionEscalation, RequestMode
from core.operator_context import OperatorContextService
from core.state import TaskStateStore
from memory.local_provider import LocalMemoryProvider
from memory.memory_store import MemoryStore
from memory.provider import HybridMemoryProvider, build_memory_backend
from memory.zep_provider import ZepMemoryProvider


class FakeOpenRouterClient:
    def is_configured(self) -> bool:
        return False

    def prompt(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        label: str | None = None,
    ) -> str:
        raise AssertionError("LLM prompt should not be used in deterministic tests.")


def answer_decision() -> AssistantDecision:
    return AssistantDecision(
        mode=RequestMode.ANSWER,
        escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
        reasoning="deterministic test",
        should_use_tools=False,
    )


class FakeUserClient:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, str]] = {}

    def get(self, user_id: str):
        if user_id not in self.users:
            raise RuntimeError("missing user")
        return SimpleNamespace(user_id=user_id)

    def add(self, *, user_id: str, first_name: str | None = None, last_name: str | None = None):
        self.users[user_id] = {"first_name": first_name or "", "last_name": last_name or ""}
        return SimpleNamespace(user_id=user_id)


class FakeThreadClient:
    def __init__(self) -> None:
        self.threads: dict[str, dict[str, object]] = {}

    def create(self, *, thread_id: str, user_id: str):
        self.threads.setdefault(thread_id, {"user_id": user_id, "messages": []})
        return SimpleNamespace(thread_id=thread_id, user_id=user_id)

    def get(self, thread_id: str, *, lastn: int | None = None, limit: int | None = None, cursor: int | None = None):
        del limit, cursor
        if thread_id not in self.threads:
            raise RuntimeError("missing thread")
        messages = list(self.threads[thread_id]["messages"])
        if lastn is not None:
            messages = messages[-lastn:]
        return SimpleNamespace(messages=messages)

    def add_messages(self, thread_id: str, *, messages, ignore_roles=None, return_context=None, request_options=None):
        del return_context, request_options
        if thread_id not in self.threads:
            raise RuntimeError("missing thread")
        for message in messages:
            self.threads[thread_id]["messages"].append(
                SimpleNamespace(
                    role=getattr(message, "role", "user"),
                    content=getattr(message, "content", ""),
                    created_at=getattr(message, "created_at", None),
                    ignored=bool(ignore_roles and getattr(message, "role", None) in ignore_roles),
                )
            )
        return SimpleNamespace(context=None)


class FakeEdgeClient:
    def __init__(self, graph_client: "FakeGraphClient") -> None:
        self.graph_client = graph_client

    def get_by_user_id(self, user_id: str, *, limit: int | None = None, uuid_cursor: str | None = None, request_options=None):
        del uuid_cursor, request_options
        edges = [edge for edge in self.graph_client.edges if edge.user_id == user_id]
        if limit is not None:
            edges = edges[:limit]
        return edges

    def delete(self, uuid_: str, *, request_options=None):
        del request_options
        self.graph_client.edges = [edge for edge in self.graph_client.edges if edge.uuid_ != uuid_]
        return SimpleNamespace(success=True)


class FakeGraphClient:
    def __init__(self) -> None:
        self.edges: list[SimpleNamespace] = []
        self.edge = FakeEdgeClient(self)
        self._counter = 0

    def add_fact_triple(
        self,
        *,
        user_id: str,
        fact: str,
        fact_name: str,
        source_node_name: str | None = None,
        target_node_name: str | None = None,
        edge_attributes=None,
        metadata=None,
        **kwargs,
    ):
        del metadata, kwargs
        self._counter += 1
        edge = SimpleNamespace(
            user_id=user_id,
            fact=fact,
            name=fact_name,
            source_node_uuid=source_node_name or "",
            target_node_uuid=target_node_name or "",
            uuid_=f"edge-{self._counter}",
            attributes=edge_attributes or {},
            created_at=edge_attributes.get("updated_at") if edge_attributes else None,
        )
        self.edges.append(edge)
        return SimpleNamespace(edge=edge, task_id=f"task-{self._counter}")

    def search(self, *, user_id: str, query: str, scope: str = "edges", limit: int = 10, **kwargs):
        del scope, kwargs
        lowered = query.lower()
        ranked = []
        for edge in self.edges:
            if edge.user_id != user_id:
                continue
            haystack = " ".join(
                [
                    str(edge.fact),
                    str(edge.attributes.get("key", "")),
                    str(edge.attributes.get("category", "")),
                    str(edge.attributes.get("layer", "")),
                ]
            ).lower()
            score = sum(1 for token in lowered.split() if token in haystack)
            if score:
                ranked.append((score, edge))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return SimpleNamespace(edges=[edge for _, edge in ranked[:limit]], context=None)


class FakeZepClient:
    def __init__(self) -> None:
        self.user = FakeUserClient()
        self.thread = FakeThreadClient()
        self.graph = FakeGraphClient()


class ZepMemoryProviderTests(unittest.TestCase):
    def test_build_memory_backend_falls_back_to_local_when_zep_is_not_configured(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "memory_backend", "hybrid"),
            patch.object(settings, "zep_api_key", None),
        ):
            backend = build_memory_backend(file_path=Path(temp_dir) / "memory.json")

        self.assertEqual(getattr(backend, "name", ""), "local_json")

    def test_hybrid_reads_durable_fact_from_zep_when_local_copy_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            local = LocalMemoryProvider(Path(temp_dir) / "memory.json")
            zep = ZepMemoryProvider(api_key="test-key", client=FakeZepClient())
            store = MemoryStore(
                provider=HybridMemoryProvider(local=local, zep=zep, backend_mode="hybrid")
            )
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that I parked on level 3 near the blue sign.")

            local.delete_fact(layer="user", key="user:parking_location")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("Where did I park?", answer_decision())

        self.assertIn("level 3", response.response.lower())
        self.assertIn("blue sign", response.response.lower())

    def test_hybrid_uses_zep_turn_history_for_continuity_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            local = LocalMemoryProvider(Path(temp_dir) / "memory.json")
            zep = ZepMemoryProvider(api_key="test-key", client=FakeZepClient())
            store = MemoryStore(
                provider=HybridMemoryProvider(local=local, zep=zep, backend_mode="hybrid")
            )
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            service.record_user_message("Please keep answers concise.")
            local.reset()

            turns = service.recent_user_turns(limit=3)

        self.assertEqual(turns, ["Please keep answers concise."])

    def test_reminder_open_loop_does_not_pollute_broad_user_memory_recall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            service = OperatorContextService(
                openrouter_client=FakeOpenRouterClient(),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )
            service.record_user_message("Remember that I prefer concise answers.")
            service.record_user_message("Remind me later to check the deployment.")

            handler = ConversationalHandler(
                openrouter_client=FakeOpenRouterClient(),
                task_store=TaskStateStore(),
                workspace_root=temp_dir,
                operator_context_service=service,
            )
            response = handler.handle("What do you remember about me?", answer_decision())

        self.assertIn("concise", response.response.lower())
        self.assertNotIn("deployment", response.response.lower())


if __name__ == "__main__":
    unittest.main()
