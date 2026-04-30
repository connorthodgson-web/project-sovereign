"""Memory Platform v2 contract, adapter, and prompt-context coverage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from core.models import AgentExecutionStatus, AgentResult, Task, TaskStatus
from core.operator_context import OperatorContextService
from core.state import TaskStateStore
from memory.contracts import OperationalStateStore, PersonalOpsStore, SemanticMemoryStore
from memory.chroma_provider import ChromaMemoryProvider
from memory.local_provider import LocalMemoryProvider
from memory.memory_store import MemoryStore
from memory.personal_ops_store import JsonPersonalOpsStore
from memory.provider import build_memory_backend


class _FakeChromaCollection:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}

    def upsert(self, *, ids, documents, metadatas) -> None:
        for item_id, document, metadata in zip(ids, documents, metadatas, strict=False):
            self.items[item_id] = {"document": document, "metadata": metadata}

    def query(self, *, query_texts, n_results, include):
        del include
        query = " ".join(query_texts).lower()
        scored = []
        for item_id, item in self.items.items():
            document = str(item["document"]).lower()
            score = 1.9
            if "short" in query and "concise" in document:
                score = 0.12
            elif "main ai" in query and "one main operator" in document:
                score = 0.14
            elif "memory" in query and "memory" in document:
                score = 0.3
            scored.append((score, item_id, item))
        scored.sort(key=lambda item: item[0])
        selected = scored[:n_results]
        return {
            "ids": [[item_id for _, item_id, _ in selected]],
            "documents": [[str(item["document"]) for _, _, item in selected]],
            "metadatas": [[item["metadata"] for _, _, item in selected]],
            "distances": [[score for score, _, _ in selected]],
        }

    def get(self, *, where=None, include=None):
        del include
        ids = []
        metadatas = []
        for item_id, item in self.items.items():
            metadata = item["metadata"]
            if where and any(metadata.get(key) != value for key, value in where.items()):
                continue
            ids.append(item_id)
            metadatas.append(metadata)
        return {"ids": ids, "metadatas": metadatas}

    def delete(self, *, ids) -> None:
        for item_id in ids:
            self.items.pop(item_id, None)


class _FakeChromaClient:
    def __init__(self) -> None:
        self.collection = _FakeChromaCollection()

    def get_or_create_collection(self, *, name: str):
        del name
        return self.collection


class _NoLlmClient:
    def is_configured(self) -> bool:
        return False


class MemoryPlatformV2Tests(unittest.TestCase):
    def test_contracts_exist_and_memory_store_exposes_v2_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")

        self.assertIsInstance(store.semantic, SemanticMemoryStore)
        self.assertIsInstance(store.operational, OperationalStateStore)
        self.assertIsInstance(store.personal_ops, PersonalOpsStore)

    def test_memory_store_facade_still_supports_existing_semantic_callers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            store.record_turn("user", "Remember that Sovereign needs clean memory contracts.")
            store.upsert_fact(
                layer="project",
                category="priority",
                key="memory-platform-v2",
                value="Memory Platform v2 separates semantic, operational, and Personal Ops stores.",
                confidence=0.95,
                source="test",
            )

            reloaded = MemoryStore(Path(temp_dir) / "memory.json")
            matches = reloaded.search_facts("memory contracts", layers=("project",))

        self.assertEqual(reloaded.list_turns(limit=1)[0].role, "user")
        self.assertTrue(any("separates semantic" in fact.value for fact in matches))

    def test_chroma_provider_semantic_search_recalls_paraphrased_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = ChromaMemoryProvider(
                local=LocalMemoryProvider(Path(temp_dir) / "memory.json"),
                client=_FakeChromaClient(),
            )
            store = MemoryStore(provider=provider)
            store.upsert_fact(
                layer="user",
                category="preference",
                key="user:response_style",
                value="You prefer concise answers.",
                confidence=0.9,
                source="test",
            )

            matches = store.search_facts("How should you keep replies short?", layers=("user",))

        self.assertTrue(any("concise answers" in fact.value for fact in matches))

    def test_chroma_provider_dedupes_semantic_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = ChromaMemoryProvider(
                local=LocalMemoryProvider(Path(temp_dir) / "memory.json"),
                client=_FakeChromaClient(),
            )
            store = MemoryStore(provider=provider)
            store.upsert_fact(
                layer="project",
                category="identity",
                key="operator-feel",
                value="Project Sovereign should feel like one main operator with a hidden team.",
                confidence=0.9,
                source="test",
            )
            store.upsert_fact(
                layer="project",
                category="identity",
                key="operator-feel-copy",
                value="Project Sovereign should feel like one main operator with a hidden team.",
                confidence=0.9,
                source="test",
            )

            matches = store.search_facts("main AI with a team", layers=("project",))

        values = [fact.value for fact in matches if "one main operator" in fact.value]
        self.assertEqual(len(values), 1)

    def test_chroma_provider_skips_secret_like_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = ChromaMemoryProvider(
                local=LocalMemoryProvider(Path(temp_dir) / "memory.json"),
                client=_FakeChromaClient(),
            )
            store = MemoryStore(provider=provider)
            store.upsert_fact(
                layer="user",
                category="preference",
                key="api-key",
                value="My API key is ghp_1234567890abcdef1234567890abcdef123456.",
                confidence=0.9,
                source="test",
            )

        self.assertEqual(store.list_facts("user"), [])
        self.assertTrue(any(action.kind == "memory_safety" for action in store.snapshot().recent_actions))

    def test_memory_provider_chroma_falls_back_to_local_when_unavailable(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(settings, "memory_provider", "chroma"),
            patch("memory.chroma_provider.chromadb", None),
        ):
            backend = build_memory_backend(file_path=Path(temp_dir) / "memory.json")

        self.assertEqual(getattr(backend, "name", ""), "local_json")

    def test_personal_ops_lists_do_not_become_semantic_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(settings, "workspace_root", temp_dir):
            semantic = MemoryStore(Path(temp_dir) / "memory.json")
            personal = JsonPersonalOpsStore(Path(temp_dir) / "personal_ops.json")
            personal.add_items("classes", ["AP Gov"])

            self.assertEqual(semantic.search_facts("AP Gov classes", layers=("user", "project")), [])
            self.assertEqual(personal.get_list("classes").items[0].text, "AP Gov")

    def test_greetings_and_secret_like_messages_are_not_durable_semantic_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            context = OperatorContextService(
                openrouter_client=_NoLlmClient(),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            context.record_user_message("hi")
            context.record_user_message("My token is sk-1234567890secret")
            snapshot = store.snapshot()

        self.assertEqual(snapshot.session_turns, [])
        self.assertEqual(snapshot.user_facts, [])
        self.assertTrue(any(action.kind == "memory_safety" for action in snapshot.recent_actions))

    def test_operational_state_is_accessible_and_not_mixed_with_user_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            task = Task(goal="Ship memory v2", title="Memory v2", description="Ship memory v2")
            result = AgentResult(
                subtask_id="check",
                agent="reviewer_agent",
                status=AgentExecutionStatus.BLOCKED,
                summary="Blocked on context tests.",
                blockers=["context tests"],
            )
            context = OperatorContextService(
                openrouter_client=_NoLlmClient(),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            context.task_started(task)
            context.task_progress(task, result)

            active_tasks = store.operational.list_active_tasks()
            open_loops = store.operational.list_open_loops()

        self.assertTrue(any("Ship memory v2" in task.goal for task in active_tasks))
        self.assertTrue(any("context tests" in loop.summary for loop in open_loops))
        self.assertEqual(store.list_facts("user"), [])
        self.assertTrue(store.list_facts("operational", category="active_task"))

    def test_compiled_prompt_context_separates_memory_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            personal = JsonPersonalOpsStore(Path(temp_dir) / "personal_ops.json")
            personal.add_items("classes", ["AP Calc", "AP Gov"])
            store.upsert_fact(
                layer="user",
                category="preference",
                key="user:response_style",
                value="You prefer concise answers.",
                confidence=0.9,
                source="test",
            )
            store.upsert_fact(
                layer="project",
                category="priority",
                key="memory-platform",
                value="Memory Platform v2 is the current Sovereign architecture priority.",
                confidence=0.9,
                source="test",
            )
            context = OperatorContextService(
                openrouter_client=_NoLlmClient(),
                memory_store_instance=store,
                personal_ops_store_instance=personal,
                task_store=TaskStateStore(),
            )

            normal = context.compile_prompt_context(
                focus_text="What is the current memory priority?",
                context_profile="task",
            )
            list_focused = context.compile_prompt_context(
                focus_text="What's on my classes list?",
                context_profile="task",
            )

        self.assertTrue(any("concise" in item for item in normal.core_memory))
        self.assertTrue(any("Memory Platform v2" in item for item in normal.retrieved_memory))
        self.assertEqual(normal.personal_ops_state, [])
        self.assertTrue(any("AP Calc" in item for item in list_focused.personal_ops_state))

    def test_runtime_prompt_block_uses_compiled_context_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            context = OperatorContextService(
                openrouter_client=_NoLlmClient(),
                memory_store_instance=MemoryStore(Path(temp_dir) / "memory.json"),
                task_store=TaskStateStore(),
            )

            block = context.build_runtime_snapshot().to_prompt_block()

        self.assertIn("compiled_prompt_context:", block)
        self.assertIn("core_memory:", block)
        self.assertIn("retrieved_memory:", block)
        self.assertIn("personal_ops_state:", block)
        self.assertIn("operational_state:", block)
        self.assertIn("short_term_state:", block)

    def test_prompt_context_remains_bounded_with_many_memory_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "memory.json")
            for index in range(30):
                store.upsert_fact(
                    layer="project",
                    category="priority",
                    key=f"priority:{index}",
                    value=f"Memory cleanup candidate {index} should not bloat prompts.",
                    confidence=0.6,
                    source="test",
                )
            context = OperatorContextService(
                openrouter_client=_NoLlmClient(),
                memory_store_instance=store,
                task_store=TaskStateStore(),
            )

            compiled = context.compile_prompt_context(
                focus_text="memory cleanup",
                context_profile="task",
            )
            block = context.build_runtime_snapshot(
                focus_text="memory cleanup",
                context_profile="task",
            ).to_prompt_block()

        self.assertLessEqual(len(compiled.core_memory), 8)
        self.assertLessEqual(len(compiled.retrieved_memory), 6)
        self.assertLess(len(block), 20000)


if __name__ == "__main__":
    unittest.main()
