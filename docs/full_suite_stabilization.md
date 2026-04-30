# Full Suite Stabilization

Date: 2026-04-28

Full-suite starting point: 370 passed, 10 failed in this workspace. The user mentioned 9 failures, but the reproduced run showed 10.

Final verification:
- `python -m pytest -q`: 380 passed
- `python -m pytest tests/test_operator_loop.py tests/test_codex_cli_agent.py tests/test_assistant_feel_behavior.py tests/test_langgraph_orchestration.py -q`: 97 passed

## Failure Review

| Test | Category | Cause | Fix | Changed |
| --- | --- | --- | --- | --- |
| `tests/test_browser_execution.py::BrowserExecutionTests::test_ambiguous_browser_request_without_url_asks_for_clarification` | Real regression | Ambiguous browser wording reached the configured LLM classifier before the local clarification guard, so it entered OpenRouter planning instead of asking what site to use. | Added a local pre-LLM ambiguous-request decision in `AssistantLayer.decide`. | Code |
| `tests/test_browser_execution.py::BrowserExecutionTests::test_browser_agent_summarizes_example_dot_com` | Real regression | When model synthesis was configured, the browser summary could paraphrase the page without preserving the visible page title used as evidence. | Grounded LLM browser synthesis by prefixing the evidence title when the model response omits it. | Code |
| `tests/test_communications_agent.py::CommunicationsAgentTests::test_email_request_still_returns_blocked` | Real regression | Gmail confirmation wording did not include the word "email", so the blocked confirmation looked less clearly tied to the email request. | Updated the confirmation prompt to say "send email with Gmail". | Code |
| `tests/test_foundation_layers.py::HonestyTests::test_communications_agent_reports_scaffolded_execution_as_blocked` | Stale/environment-sensitive expectation | The test relied on default Gmail being unavailable, but this workspace can have Gmail configured. | Made the test explicitly patch Gmail disabled, preserving the scaffolded-unavailable behavior it is meant to cover. | Test |
| `tests/test_manual_behavior_stabilization.py::ManualBehaviorStabilizationTests::test_calendar_token_missing_returns_oauth_setup_before_confirmation` | Real regression | Calendar setup wording humanized "OAuth" into "Google sign-in", which made the needed setup less precise. | Kept OAuth visible in calendar readiness wording. | Code |
| `tests/test_manual_behavior_stabilization.py::ManualBehaviorStabilizationTests::test_email_unavailable_fails_fast_without_planner_or_progress_ack` | Stale/environment-sensitive expectation | The test relied on ambient Gmail being unavailable and expected older backend-ish wording. | Patched Gmail disabled in the test and updated the assertion to the current user-facing setup/OAuth wording. | Test |
| `tests/test_model_routing.py::StructuredModelRoutingTests::test_memory_fast_path_uses_no_heavy_model` | Real regression | Direct memory questions only used the obvious fast path after the heavy model attempt. | Moved obvious assistant/memory fast-path detection ahead of the LLM call. | Code |
| `tests/test_objective_loop.py::ObjectiveLoopTests::test_disabled_premium_tools_are_blocked_even_if_llm_asks` | Stale wording expectation | The blocker was correctly enforced, but deterministic humanization lowercased "Manus". | Preserved the product/tool name capitalization while keeping the blocker human-readable. | Code |
| `tests/test_objective_loop.py::ObjectiveLoopTests::test_final_response_includes_evidence_summary_not_backend_logs` | Real regression | Browser-plus-file requests were captured by the browser fast path, so the file save never entered the objective loop. | Classified browser evidence plus saved-file requests as bounded execution and excluded them from browser/file fast actions. | Code |
| `tests/test_objective_loop.py::ObjectiveLoopTests::test_multi_step_browser_file_request_enters_llm_led_loop` | Real regression | Same root cause: the direct browser fast path short-circuited the objective loop and never called the decision maker. | Same bounded-execution classification and fast-action exclusion fix. | Code |

## Remaining Risks

- Gmail behavior remains intentionally environment-sensitive when real Gmail is configured; unavailable-Gmail tests now patch the disabled state explicitly.
- The browser title-prefix guard is deliberately conservative and only adds evidence already captured by the browser tool.
- The browser-plus-file heuristic is narrow: it targets explicit browser requests that also ask to save/write/create/put a file or summary. Broader multi-artifact workflows should still be handled by the planner/objective loop rather than expanding `fast_actions.py`.
- No safety or evidence gates were loosened. Email sending still requires confirmation, disabled Manus remains blocked, and browser/file completion now requires saved artifact evidence.
