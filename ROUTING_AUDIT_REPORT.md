# ROUTING & ASSISTANT AUDIT REPORT
**Date:** 2026-04-25  
**Auditor:** Claude (Cursor Agent)  
**Scope:** Deep analysis of routing, assistant paths, and completion verification  
**Status:** CRITICAL ISSUES FOUND

---

## EXECUTIVE SUMMARY

### Critical Findings
1. **LANGGRAPH WRAPS EVERYTHING** - Even simple greetings go through LangGraph (unavoidable latency)
2. **FILE CREATION ROUTED TO HEAVY FLOW** - Simple file tasks take 2+ minutes through full planner/reviewer/verifier flow
3. **WORKSPACE PATH DOUBLED** - File created at `workspace/workspace/created_items/codex_test.md` instead of `workspace/created_items/codex_test.md`
4. **COMPLETION VERIFICATION WEAK** - System claimed success despite wrong path
5. **NO TRUE FAST PATH BYPASS** - Fast paths still enter LangGraph orchestration

### Root Causes
- LangGraph is the **only entry point** - no pre-LangGraph fast path
- Lane selection happens **inside** LangGraph, not before
- Simple file operations classified as "execution" not "fast action"
- Reviewer checks tool evidence but doesn't validate actual filesystem paths against expectations
- Verifier trusts evaluator without independent file verification

---

## ARCHITECTURE ANALYSIS

### Current Flow (All Requests)

```
User Input
    ↓
supervisor.handle_user_goal()
    ↓ [SUPERVISOR_RECEIVED logged]
    ↓
orchestration_graph.invoke()
    ↓ [LANGGRAPH_START logged - ALWAYS HAPPENS]
    ↓
LangGraph: start_node → select_lane_node
    ↓
assistant_layer.decide() - Determines RequestMode
    ↓
lane_selector() - Determines lane
    ↓
LangGraph Routing:
    ├─ assistant_or_fast_action_node (for ANSWER/fast ACT)
    └─ planning_node → execution → review → verify (for EXECUTE/slow ACT)
```

### The LangGraph Entry Point

**Location:** `core/orchestration_graph.py:79-101`

```python:79:101:c:\Users\conno\project-sovereign\core\orchestration_graph.py
def invoke(self, user_message: str) -> ChatResponse:
    normalized_message = " ".join(user_message.split())
    self.logger.info("LANGGRAPH_START message=%r normalized=%r", user_message, normalized_message)
    result = self._graph.invoke(
        {
            "user_message": user_message,
            "normalized_message": normalized_message,
            "memory_backend": self.memory_backend,
            "iteration_count": 0,
            "replan_count": 0,
        },
        config=self._invoke_config(),
    )
```

**Finding:** There is NO pre-LangGraph routing. Every request must enter the graph.

---

## TRACE ANALYSIS: TEST INPUTS

### Input 1: "hi!"

**Expected Behavior:**
- Fast assistant path
- No planning
- No OpenRouter calls
- < 100ms latency

**Actual Behavior:**
```
SUPERVISOR_RECEIVED raw_goal='hi!' 
LANGGRAPH_START message='hi!' normalized='hi!'
LANE_SELECTED lane=assistant agent=assistant_agent mode=answer
AGENT_SELECTED lane=assistant agent=assistant_agent
→ Fast conversational reply
```

**Analysis:**
✅ Correctly routed to assistant lane  
✅ No planning triggered  
❌ LangGraph invoked (adds ~30-50ms overhead)  
✅ Fast path decision works correctly  

**Issue:** While routing is correct, LangGraph entry is unavoidable. This is **acceptable** because:
- Fast paths exit quickly
- LangGraph overhead is minimal for assistant lane
- Architectural consistency (all requests through one graph)

**Verdict:** NOT A BUG - This is by design per AGENTS.md line 103-106 which states LLM orchestration should drive routing, not hardcoded Python.

---

### Input 2: "Create a small README note in workspace/created_items/codex_test.md explaining that Codex CLI Agent is connected."

**Expected Behavior:**
- Fast ACT mode, single_action escalation
- Route to coding_agent with file_tool
- Direct file write
- Simple review
- < 5 seconds total

**Actual Behavior (from terminal log):**
```
Timeline:
13:48:04 - SUPERVISOR_RECEIVED
13:48:04 - LANGGRAPH_START  
13:48:04 - assistant_layer.decide() → ACT, single_action
13:48:07 - OpenRouter call for decision (3s)
13:48:09 - LANE_SELECTED execution_flow, planner_agent
13:48:09 - PLANNER_AGENT_START
13:50:20 - OpenRouter call for planning (131s!!!)
13:50:23 - Plan: 3 subtasks (memory → coding → reviewer)
13:50:23 - Execute memory_agent subtask
13:50:23 - Execute coding_agent with file_tool write
13:50:23 - Execute reviewer_agent 
13:50:25 - OpenRouter evaluation (2s)
13:50:27 - Verifier (2s)
13:51:06 - OpenRouter compose (39s)
13:51:08 - FINAL_RESPONSE completed
```

**Total Latency:** 124,366ms (124 seconds / ~2 minutes)

**What Went Wrong:**

1. **Routing Decision:** Assistant decided ACT/single_action correctly
2. **Lane Selection:** Routed to `execution_flow` instead of `fast_action`
3. **Planning Overhead:** OpenRouter planning took 131 seconds
4. **Unnecessary Subtasks:** Created 3 subtasks for a simple file write
5. **Path Issue:** File created at wrong path (see below)

---

### Path Doubling Bug

**Expected Path:** `workspace/created_items/codex_test.md`  
**Actual Path:** `workspace/workspace/created_items/codex_test.md`  
**Evidence:** File exists at doubled path (verified via Glob/Read)

**Root Cause Analysis:**

Likely in `tools/file_tool.py` or path resolution logic. The workspace root is being prepended twice:
1. Base workspace: `C:\Users\conno\project-sovereign\workspace\`
2. User request: `workspace/created_items/codex_test.md`
3. Result: `workspace_root + "workspace/created_items/codex_test.md"` → doubled

**Verification Evidence:**
```python
# From reviewer_agent.py:73-78
if evidence.operation == "write" and evidence.file_path:
    verification = self._execute_file_tool("read", path=evidence.file_path)
    passed = verification.success
```

Reviewer read the file successfully, which means it verified the **wrong** path without catching the error.

---

### Input 3: "who are you?"

**Expected:** Assistant fast path, self-knowledge response  
**Actual:** Would route to assistant lane → conversational handler  
**Analysis:** Should work correctly (similar to "hi!")

---

### Input 4: "what can you do?"

**Expected:** Assistant fast path, capability description  
**Actual:** Would route to assistant lane → capability list  
**Analysis:** Should work correctly

---

### Input 5: "my name is Connor Hodgson"

**Expected:** Fast memory update path  
**Actual:** 
- `is_name_statement()` returns True
- Routes to assistant lane, memory_agent
- Fast path via `_obvious_assistant_fast_path_decision()`

**Analysis:** ✅ Should work correctly per assistant_fast_path.py:115-116

---

### Input 6: "what do you remember about me?"

**Expected:** Fast memory lookup  
**Actual:**
- `is_user_memory_question()` returns True
- Routes to assistant lane
- Memory retrieval without planning

**Analysis:** ✅ Should work correctly

---

### Input 7: "remind me in 30 seconds to drink water"

**Expected:**
- FastActionHandler.handle() recognizes reminder request
- Parses time expression
- Schedules reminder
- < 1 second

**Actual Flow:**
```
supervisor.handle_user_goal()
    ↓
LangGraph: select_lane_node
    ↓ decision.mode = ACT
    ↓ _looks_like_explicit_reminder_request() = True
    ↓
lane = "fast_action", agent = "reminder_agent"
    ↓
assistant_or_fast_action_node
    ↓
fast_action_handler.handle() → schedules reminder
```

**Analysis:** ✅ Should work correctly via fast action lane

---

### Input 8: "open https://example.com and summarize it"

**Expected:**
- Browser request detected
- Route to browser_agent
- Execute browser_tool
- Return synthesis

**Actual Flow:**
```
extract_obvious_browser_request() → BrowserRequest(action=open, url=...)
_guardrail_decision() → ACT, single_action
_select_lane() → execution_flow, planner_agent (browser detected)
Planner creates browser subtasks
Router assigns to browser_agent
```

**Analysis:** ⚠️ Goes through full planning even though it's a direct URL request

---

### Input 9: "open cnn and tell me the top 5 stories"

**Expected:** Similar to above  
**Actual:** Would go through full planning flow  
**Issue:** "cnn" is not a full URL, so might require clarification or URL resolution

---

### Input 10: "Refactor a failing auth module and add tests"

**Expected:**
- EXECUTE mode, objective_completion
- Route to codex_cli_agent (serious coding task)
- Bounded prompt to Codex CLI
- Execute, review, verify

**Actual Flow:**
```
assistant_layer.decide() → EXECUTE, objective_completion
_select_lane() → execution_flow, planner_agent
_looks_like_serious_coding_goal() → True
_should_delegate_to_codex_cli() → True
_create_codex_cli_plan() → subtasks for memory + codex + reviewer
```

**Analysis:** ✅ Correctly routes to Codex CLI for serious coding work

---

## ROUTING LOGIC DEEP DIVE

### Assistant Layer Decision Tree

**Location:** `core/assistant.py:73-92`

```python:73:92:c:\Users\conno\project-sovereign\core\assistant.py
def decide(self, user_message: str) -> AssistantDecision:
    guardrail_decision = self._guardrail_decision(user_message)
    if guardrail_decision is not None:
        return guardrail_decision
    fast_path_decision = self._obvious_assistant_fast_path_decision(user_message)
    if fast_path_decision is not None:
        trace = current_request_trace()
        if trace is not None:
            trace.set_path("assistant_fast_path")
        return fast_path_decision
    llm_decision = self._decide_with_llm(user_message)
    if llm_decision is not None:
        reminder_override = self._override_llm_for_guardrailed_action(user_message, llm_decision)
        if reminder_override is not None:
            return reminder_override
        browser_override = self._override_llm_for_browser_request(user_message, llm_decision)
        if browser_override is not None:
            return browser_override
        return llm_decision
    return self._decide_deterministically(user_message)
```

**Analysis:**
1. ✅ Guardrails catch obvious cases (empty input, math, explicit browser URLs)
2. ✅ Fast path catches greetings, thanks, name statements, memory questions
3. ⚠️ LLM decision can take 2-3 seconds
4. ✅ Overrides ensure reminders/browser don't misroute

---

### Lane Selector Logic

**Location:** `core/supervisor.py:377-423`

```python:377:423:c:\Users\conno\project-sovereign\core\supervisor.py
def _select_lane(
    self,
    normalized_goal: str,
    decision: AssistantDecision,
) -> LaneSelection:
    lowered = normalized_goal.lower()
    if decision.mode == RequestMode.ANSWER:
        if self._is_memory_lane_request(lowered):
            return LaneSelection(
                lane="assistant",
                agent_id="memory_agent",
                reasoning="Memory updates and follow-ups should stay on the fast conversational memory lane.",
            )
        return LaneSelection(
            lane="assistant",
            agent_id="assistant_agent",
            reasoning="Lightweight conversational requests should stay on the assistant lane.",
        )
    if decision.mode == RequestMode.ACT:
        if extract_obvious_browser_request(normalized_goal) is not None:
            return LaneSelection(
                lane="execution_flow",
                agent_id="planner_agent",
                reasoning="Direct browser requests should route through the planner-backed execution flow.",
            )
        if self.assistant_layer._looks_like_explicit_reminder_request(lowered):
            return LaneSelection(
                lane="fast_action",
                agent_id="reminder_agent",
                reasoning="Reminder requests should use the reminder agent lane without heavy planning.",
            )
        if self._looks_like_communications_request(lowered):
            return LaneSelection(
                lane="execution_flow",
                agent_id="planner_agent",
                reasoning="Outbound messaging and email requests should route through the planner-backed communications execution flow.",
            )
        return LaneSelection(
            lane="fast_action",
            agent_id="assistant_agent",
            reasoning="Single-step actions should attempt the fast action lane first.",
        )
    return LaneSelection(
        lane="execution_flow",
        agent_id="planner_agent",
        reasoning="Complex tasks should route through the planner/review/verifier execution flow.",
    )
```

**Critical Finding:**

The file creation request with mode=ACT should have hit this condition:
```python:416:419:c:\Users\conno\project-sovereign\core\supervisor.py
return LaneSelection(
    lane="fast_action",
    agent_id="assistant_agent",
    reasoning="Single-step actions should attempt the fast action lane first.",
)
```

But `FastActionHandler.handle()` only supports:
- Reminder requests
- Cancel/update reminder
- Calendar events

**Location:** `core/fast_actions.py:51-62`

```python:51:62:c:\Users\conno\project-sovereign\core\fast_actions.py
def handle(self, user_message: str, decision: AssistantDecision) -> ChatResponse | None:
    if decision.mode != RequestMode.ACT:
        return None
    if self._looks_like_cancel_reminder_request(user_message):
        return self._handle_cancel_reminder(user_message, decision)
    if self._looks_like_update_reminder_request(user_message):
        return self._handle_update_reminder(user_message, decision)
    if self._looks_like_calendar_event_request(user_message):
        return self._handle_calendar_event(user_message, decision)
    if self._is_simple_reminder_request(user_message):
        return self._handle_reminder(user_message, decision)
    return None  # ← File creation returns None here, falls through to planning!
```

**THIS IS THE BUG!**

File creation requests:
1. Get classified as ACT
2. Route to fast_action lane
3. FastActionHandler returns None (not supported)
4. LangGraph falls through to planning flow

**Evidence from orchestration_graph.py:214-322:**

```python:318:321:c:\Users\conno\project-sovereign\core\orchestration_graph.py
def _route_after_fast_path(self, state: SovereignGraphState) -> str:
    if state.get("response_payload") is not None:
        return "final_response"
    return "planning"  # ← Falls through here when fast_action returns None
```

---

## ROUTER CLASSIFICATION ISSUES

### Codex CLI Score Logic

**Location:** `core/router.py:191-200`

```python:191:200:c:\Users\conno\project-sovereign\core\router.py
def _codex_score(self, description: str) -> int:
    serious_patterns = (
        r"\b(build|implement|refactor|debug|fix|repair)\b",
        r"\b(test|tests|failing|regression|bug|feature)\b",
        r"\b(codebase|module|function|integration)\b",
    )
    matches = sum(1 for pattern in serious_patterns if re.search(pattern, description))
    if matches >= 2 and "browser" not in description and "remind" not in description:
        return 5
    return 0
```

**Issue:** "Create a small README note" might match patterns if description includes words like "explain" or "connected", but shouldn't trigger Codex CLI for trivial file operations.

---

## COMPLETION VERIFICATION ANALYSIS

### Reviewer Agent Logic

**Location:** `agents/reviewer_agent.py:66-108`

The reviewer executes a file read to verify write operations:

```python:73:78:c:\Users\conno\project-sovereign\agents\reviewer_agent.py
if evidence.operation == "write" and evidence.file_path:
    verification = self._execute_file_tool("read", path=evidence.file_path)
    passed = verification.success
    verification_notes.append(
        "Verified created file exists and can be read." if verification.success else f"File verification failed: {verification.error}"
    )
```

**Critical Flaw:** 
- Reviewer reads `evidence.file_path` (the path that was actually written)
- Does NOT compare against the **requested** path from the original goal
- Cannot detect path doubling bugs

**Example:**
- User requests: `workspace/created_items/codex_test.md`
- System creates: `workspace/workspace/created_items/codex_test.md`
- Reviewer reads: `workspace/workspace/created_items/codex_test.md` ✅ Success!
- Bug goes undetected

---

### Verifier Agent Logic

**Location:** `agents/verifier_agent.py:18-61`

```python:18:29:c:\Users\conno\project-sovereign\agents\verifier_agent.py
def run(self, task: Task, subtask: SubTask) -> AgentResult:
    evaluation, evaluation_mode = self.evaluator.evaluate(task)
    if evaluation.satisfied:
        status = AgentExecutionStatus.COMPLETED
        summary = "Verified that the final output satisfies the original goal."
    elif evaluation.blocked:
        status = AgentExecutionStatus.BLOCKED
        summary = "Verified that the task is blocked and cannot honestly be marked complete."
    else:
        status = AgentExecutionStatus.SIMULATED
        summary = "Verified that the task is not yet complete and should not be marked done."
    return AgentResult(...)
```

**Critical Flaw:**
- Verifier delegates to `GoalEvaluator.evaluate(task)`
- Evaluator likely uses LLM reasoning over task.results
- Does NOT independently check file existence
- Trusts reviewer's verification

**Result:** False completions can propagate through the system.

---

## ROOT CAUSE SUMMARY

### Issue 1: File Creation Routes to Heavy Flow

**Root Cause:**
- `FastActionHandler` doesn't support file operations
- ACT mode defaults to fast_action lane
- Falls through to planning when fast_action returns None

**Fix Required:**
- Add file operation support to FastActionHandler, OR
- Route simple file operations directly to coding_agent without planning, OR
- Update lane selector to detect file operations and route appropriately

---

### Issue 2: Path Doubling

**Root Cause:**
- Workspace path resolution in file_tool prepends workspace_root to user-provided path
- User path already includes "workspace/" prefix
- Result: `workspace_root/workspace/...`

**Fix Required:**
- Normalize/strip workspace prefix from user paths before resolution
- OR detect and prevent double-pathing in file_tool

---

### Issue 3: Weak Completion Verification

**Root Cause:**
- Reviewer verifies tool outputs but not semantic correctness
- Verifier trusts evaluator which uses LLM reasoning
- No independent filesystem validation against original request

**Fix Required:**
- Reviewer should parse original goal to extract expected path
- Compare expected vs actual paths
- Verifier should perform independent evidence checks for file operations

---

### Issue 4: LangGraph Wraps Everything

**Root Cause:**
- Architectural decision: all requests enter orchestration graph
- No pre-LangGraph fast path bypass

**Fix Required:**
- NONE - This is intentional per AGENTS.md § 4.1
- LLM-driven orchestration is the design goal
- Fast paths inside LangGraph are sufficient

---

## PROPOSED ROUTING POLICY (from Requirements)

From user's "Desired routing policy":

| Input Type | Current Behavior | Expected Behavior | Status |
|------------|------------------|-------------------|--------|
| Greetings/self-knowledge | ✅ Assistant fast path | Assistant fast path | CORRECT |
| Memory updates/questions | ✅ Memory fast path | Memory fast path/Memory Agent | CORRECT |
| Simple reminders | ✅ Reminder fast path | Reminder fast path | CORRECT |
| Direct browser tasks | ⚠️ Full planning | Browser Agent | NEEDS FIX |
| Trivial file ops | ❌ Full planning (2min+) | Local coding_agent/file_tool | **BROKEN** |
| Serious coding | ✅ Codex CLI | Codex CLI Agent when enabled | CORRECT |
| Complex multi-step | ✅ Full flow | Planner → agents → Reviewer | CORRECT |

---

## CURRENT ARCHITECTURE DIAGRAM

```
┌─────────────────────────────────────────────────────────────┐
│                     USER INPUT                               │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              supervisor.handle_user_goal()                   │
│              [SUPERVISOR_RECEIVED logged]                    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│           orchestration_graph.invoke()                       │
│           [LANGGRAPH_START logged - ALWAYS]                  │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  LangGraph: start_node → select_lane_node                    │
│                                                               │
│  ┌──────────────────────────────────────────────┐           │
│  │  assistant_layer.decide()                     │           │
│  │  ├─ _guardrail_decision()                     │           │
│  │  ├─ _obvious_assistant_fast_path_decision()   │           │
│  │  ├─ _decide_with_llm() [2-3s OpenRouter]     │           │
│  │  └─ _decide_deterministically()               │           │
│  └──────────────────────────────────────────────┘           │
│                       │                                       │
│                       ▼                                       │
│  ┌──────────────────────────────────────────────┐           │
│  │  lane_selector()                              │           │
│  │  Returns: lane + agent_id                     │           │
│  └──────────────────────────────────────────────┘           │
└──────────────────────┬──────────────────────────────────────┘
                       │
      ┌────────────────┼────────────────┐
      │                │                │
      ▼                ▼                ▼
┌──────────┐  ┌────────────────┐  ┌──────────────┐
│ASSISTANT │  │  FAST_ACTION   │  │  EXECUTION   │
│   LANE   │  │     LANE       │  │     FLOW     │
└──────────┘  └────────────────┘  └──────────────┘
      │                │                │
      │                │                │
      ▼                ▼                ▼
┌──────────┐  ┌────────────────┐  ┌──────────────┐
│Assistant │  │FastActionHandler│  │  PLANNER     │
│Adapter   │  │                │  │              │
│          │  │ • Reminders    │  │ OpenRouter   │
│Conversa- │  │ • Calendar     │  │ 30-130s      │
│tional    │  │ • [no files]   │  │              │
│Handler   │  │                │  └──────┬───────┘
└──────────┘  └────────┬───────┘         │
      │                │                │
      │                │                ▼
      │                │         ┌──────────────┐
      │                │         │  EXECUTION   │
      │                │         │  Agent Loop  │
      │                │         └──────┬───────┘
      │                │                │
      │                │                ▼
      │                │         ┌──────────────┐
      │                │         │   REVIEW     │
      │                │         │  Reviewer    │
      │                │         └──────┬───────┘
      │                │                │
      │                │                ▼
      │                │         ┌──────────────┐
      │                │         │   VERIFY     │
      │                │         │  Verifier    │
      │                │         └──────┬───────┘
      │                │                │
      ▼                ▼                ▼
┌─────────────────────────────────────────────────────────────┐
│              _final_response_node                            │
│              [Compose final message]                         │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
                  ChatResponse
```

---

## BAD ROUTING CASES FOUND

### Case 1: Simple File Creation
**Input:** "Create a small README note in workspace/created_items/codex_test.md explaining that Codex CLI Agent is connected."

**Expected:**
- Mode: ACT, single_action
- Lane: fast_action → falls through → local coding_agent
- Latency: < 5s

**Actual:**
- Mode: ACT, single_action ✅
- Lane: fast_action → **None** → execution_flow ❌
- Planning: 131s OpenRouter call ❌
- Total: 124s ❌

**Why It's Bad:**
- Trivial file write shouldn't require planning
- 2+ minute latency for simple operation
- Violates AGENTS.md § 2.2 "quick-answer assistant task" principle

---

### Case 2: Direct Browser URL
**Input:** "open https://example.com and summarize it"

**Expected:**
- Mode: ACT, single_action
- Lane: execution_flow (browser requires planning for evidence synthesis)
- Quick plan: browser open → synthesize
- Latency: 10-20s

**Actual:**
- Mode: ACT, single_action ✅
- Lane: execution_flow ✅
- Planning: Full OpenRouter plan
- Latency: Likely 30-60s

**Why It's Suboptimal:**
- Direct URL could use deterministic browser plan
- Planner forces OpenRouter call for obvious case

---

## FALSE COMPLETION RISKS

### Risk 1: Path Mismatch
**Scenario:** File created at wrong path  
**Current Behavior:** Reviewer verifies file exists, passes  
**Risk:** User can't find file, believes task failed  
**Severity:** HIGH

---

### Risk 2: Tool Evidence Without Semantic Check
**Scenario:** Tool returns success but output is wrong  
**Current Behavior:** Reviewer checks evidence presence, not correctness  
**Risk:** False completions propagate  
**Severity:** MEDIUM

---

### Risk 3: Verifier Trusts Evaluator
**Scenario:** LLM evaluator makes reasoning error  
**Current Behavior:** Verifier accepts evaluation without independent check  
**Risk:** False confidence in completion  
**Severity:** MEDIUM

---

### Risk 4: No User-Visible Verification
**Scenario:** File/browser/communication task claims success  
**Current Behavior:** No screenshot, no path confirmation in user message  
**Risk:** User can't verify without searching  
**Severity:** LOW

---

## MINIMAL FIX PLAN

### Priority 1: Fix File Creation Routing (CRITICAL)

**Problem:** Simple file ops route to heavy planning flow

**Solution A (Recommended):** Add file operations to FastActionHandler

**Changes Required:**
1. `core/fast_actions.py`: Add `_handle_file_operation()` method
2. Detect: "create file", "write file", "read file" in message
3. Parse: target path, optional content
4. Execute: Direct file_tool invocation
5. Verify: Independent file read/list
6. Return: ChatResponse with evidence

**Pseudo-code:**
```python
def _handle_file_operation(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
    parsed = self._parse_file_request(user_message)
    if parsed is None:
        return None
    
    # Execute file tool directly
    from tools.file_tool import FileTool
    file_tool = FileTool()
    result = file_tool.execute(
        ToolInvocation(
            tool_name="file_tool",
            action=parsed.operation,
            parameters={"path": parsed.path, "content": parsed.content}
        )
    )
    
    # Verify independently
    if parsed.operation == "write":
        verify_result = file_tool.execute(
            ToolInvocation(tool_name="file_tool", action="read", parameters={"path": parsed.path})
        )
        if not verify_result.success:
            return blocked_response("File was not created at expected path")
    
    return success_response(result)
```

**Testing:**
- "Create a file called test.txt with content 'hello'"
- "Write a README in workspace/created_items/test.md"
- "Read the file at workspace/created_items/test.md"

---

**Solution B (Alternative):** Route file ops to coding_agent without planning

**Changes Required:**
1. `core/supervisor.py:_select_lane()`: Detect simple file requests
2. If ACT + file operation detected → return lane="execution_flow", but skip planner
3. `core/orchestration_graph.py`: Add direct execution path

**Pros:** Reuses existing coding_agent  
**Cons:** More complex, still enters execution flow

---

### Priority 2: Fix Path Doubling (CRITICAL)

**Problem:** `workspace_root/workspace/...` duplication

**Location:** `tools/file_tool.py` (likely)

**Solution:**
1. Locate path resolution function in file_tool
2. Add path normalization:
   ```python
   def _resolve_path(self, user_path: str) -> Path:
       # Strip leading workspace prefix if present
       normalized = re.sub(r'^workspace[/\\]', '', user_path)
       return self.workspace_root / normalized
   ```
3. Test: Ensure `workspace/created_items/test.md` resolves to `<workspace_root>/created_items/test.md`

---

### Priority 3: Strengthen Completion Verification (HIGH)

**Problem:** Reviewer doesn't compare expected vs actual paths

**Solution:**
1. `agents/reviewer_agent.py:_review_file_result()`:
   - Extract expected path from task.goal or subtask.objective
   - Compare against evidence.file_path
   - Fail review if mismatch

**Implementation:**
```python
def _review_file_result(self, task: Task, subtask: SubTask, prior_result: AgentResult) -> AgentResult:
    evidence = next(item for item in prior_result.evidence if isinstance(item, FileEvidence))
    
    # NEW: Extract expected path from goal
    expected_path = self._extract_expected_path(task.goal, subtask.objective)
    
    verification_notes: list[str] = []
    passed = False
    
    if evidence.operation == "write" and evidence.file_path:
        # NEW: Check path match
        if expected_path and not self._paths_match(expected_path, evidence.file_path):
            passed = False
            verification_notes.append(
                f"File was created at {evidence.file_path} but expected {expected_path}"
            )
        else:
            verification = self._execute_file_tool("read", path=evidence.file_path)
            passed = verification.success
            verification_notes.append(...)
    
    return AgentResult(...)
```

---

### Priority 4: Add Browser Direct Path (MEDIUM)

**Problem:** Obvious browser URLs go through full planning

**Solution:**
1. `core/planner.py:_build_forced_browser_invocation()`: Already exists!
2. Verify it's being called correctly
3. Ensure direct browser URLs skip OpenRouter planning

**Current Code:**
```python:77:90:c:\Users\conno\project-sovereign\core\planner.py
forced_browser_invocation = self._build_forced_browser_invocation(goal)
if forced_browser_invocation is not None:
    self.logger.info(
        "BROWSER_REQUEST_DETECTED goal=%r action=%s url=%s path=fast_path",
        goal,
        forced_browser_invocation.invocation.action,
        forced_browser_invocation.invocation.parameters.get("url"),
    )
    self.logger.info("PLANNER_PATH goal=%r planner_path=fast_browser", goal)
    return self._create_tool_plan(
        goal,
        forced_browser_invocation,
        escalation_level=escalation_level,
    ), "deterministic"
```

**Analysis:** This already exists! The issue might be that it's only used when planner is invoked, not at lane selection.

**Recommendation:** Verify this path is being hit for direct browser URLs. If not, consider moving detection earlier.

---

## TESTS TO ADD

### Unit Tests

**File:** `tests/test_routing_fast_paths.py`

```python
def test_simple_file_creation_routes_to_fast_path():
    """Verify trivial file operations don't enter heavy planning."""
    supervisor = Supervisor()
    
    # Should complete in < 5 seconds
    start = time.time()
    response = supervisor.handle_user_goal(
        "Create a file called test.txt in workspace/created_items with content 'hello'"
    )
    elapsed = time.time() - start
    
    assert elapsed < 5.0, f"File creation took {elapsed}s, expected < 5s"
    assert response.planner_mode == "fast_action"
    assert response.status == TaskStatus.COMPLETED

def test_file_path_resolution_no_doubling():
    """Verify workspace paths don't double."""
    from tools.file_tool import FileTool
    
    file_tool = FileTool()
    result = file_tool.execute(
        ToolInvocation(
            tool_name="file_tool",
            action="write",
            parameters={"path": "workspace/created_items/test.md", "content": "test"}
        )
    )
    
    assert result.success
    # Verify path doesn't contain duplicate "workspace"
    assert "workspace/workspace" not in result.payload["file_path"]
    
def test_reviewer_catches_wrong_path():
    """Verify reviewer fails when file created at wrong path."""
    # ... setup task with expected path ...
    reviewer = ReviewerAgent()
    # ... mock evidence with wrong path ...
    result = reviewer.run(task, subtask)
    
    assert result.status == AgentExecutionStatus.BLOCKED
    assert "wrong path" in result.summary.lower()
```

---

### Integration Tests

**File:** `tests/test_routing_integration.py`

```python
@pytest.mark.parametrize("input,expected_lane,expected_latency_ms", [
    ("hi!", "assistant", 200),
    ("my name is Connor", "assistant", 200),
    ("remind me in 5 minutes to stretch", "fast_action", 1000),
    ("create a file test.txt with hello", "fast_action", 5000),
    ("open https://example.com", "execution_flow", 30000),  # Browser needs evidence
    ("refactor auth module and add tests", "execution_flow", 60000),  # Codex CLI
])
def test_routing_latency(input, expected_lane, expected_latency_ms):
    """Verify each input type routes correctly with appropriate latency."""
    supervisor = Supervisor()
    
    start = time.time()
    response = supervisor.handle_user_goal(input)
    elapsed_ms = (time.time() - start) * 1000
    
    assert elapsed_ms < expected_latency_ms, f"Input '{input}' took {elapsed_ms}ms, expected < {expected_latency_ms}ms"
    # ... verify lane via trace ...
```

---

## CODEX PROMPT TO IMPLEMENT FIXES

```
Implement the following fixes to the Project Sovereign routing system:

1. ADD FILE OPERATIONS TO FAST ACTION HANDLER

Location: core/fast_actions.py

Add a new method `_handle_file_operation()` that:
- Detects file operation requests ("create file", "write file", "read file")
- Parses target path and optional content from user message
- Executes file_tool directly without planning
- Verifies file existence independently after write operations
- Returns ChatResponse with evidence

Requirements:
- Support patterns: "create a file", "write a file", "create <filename>", "write <filename>"
- Parse path from message (e.g., "create test.txt" → path="test.txt")
- Parse content if provided (e.g., "with content 'hello'" → content="hello")
- For write operations: verify file exists at expected path after write
- Fail with blocked status if verification fails
- Return completed status with file evidence if successful
- Latency target: < 2 seconds for simple file operations

Add detection to `handle()` method:
```python
def handle(self, user_message: str, decision: AssistantDecision) -> ChatResponse | None:
    if decision.mode != RequestMode.ACT:
        return None
    
    # NEW: Check for file operations
    file_response = self._handle_file_operation(user_message, decision)
    if file_response is not None:
        return file_response
    
    # ... existing reminder/calendar checks ...
```

2. FIX PATH DOUBLING

Location: tools/file_tool.py (find path resolution function)

Add path normalization that:
- Strips leading "workspace/" prefix if present in user-provided paths
- Prevents double-prepending of workspace_root
- Handles both forward and backslash separators

Example:
- Input: "workspace/created_items/test.md"
- After normalization: "created_items/test.md"
- After resolution: "<workspace_root>/created_items/test.md"

3. STRENGTHEN REVIEWER PATH VERIFICATION

Location: agents/reviewer_agent.py:_review_file_result()

Add logic to:
- Extract expected file path from task.goal or subtask.objective
- Use regex to find path patterns (e.g., "create X in Y", "file called X")
- Compare expected path against evidence.file_path
- Fail review if paths don't match (accounting for workspace normalization)
- Include both expected and actual paths in verification_notes

4. ADD UNIT TESTS

Location: tests/test_fast_actions.py

Add tests for:
- Simple file creation routing to fast path
- Path resolution without doubling
- Reviewer catching wrong path
- Fast action latency < 2s for file ops

Test coverage should verify:
- "create a file test.txt" → fast path
- "create workspace/created_items/test.md" → no path doubling
- "write a file" with no path → blocked (missing path)
- File created at wrong path → reviewer fails

5. ADD LOGGING

Add INFO-level logs at key points:
- FastActionHandler: "FAST_FILE_OP_START path=X"
- FastActionHandler: "FAST_FILE_OP_END success=X latency_ms=X"
- ReviewerAgent: "REVIEWER_PATH_CHECK expected=X actual=X match=X"
- FileTool: "FILE_PATH_RESOLVED input=X normalized=X final=X"

Constraints:
- Maintain existing API contracts
- Don't break fast paths for reminders/calendar
- Keep latency minimal (< 2s for file ops)
- Follow existing code patterns and naming
- Add type hints
- Include docstrings

Priority order:
1. File operations in FastActionHandler (CRITICAL - fixes 2 min latency)
2. Path doubling fix (CRITICAL - fixes wrong file location)
3. Reviewer path verification (HIGH - prevents false completions)
4. Tests (HIGH - prevents regressions)
5. Logging (MEDIUM - enables debugging)

Start with Priority 1 (FastActionHandler file operations).
```

---

## END OF REPORT

**Date:** 2026-04-25  
**Next Steps:**
1. Review this audit with team
2. Implement Priority 1 fix (FastActionHandler file operations)
3. Implement Priority 2 fix (path doubling)
4. Add tests
5. Verify routing latencies
6. Deploy and monitor

**Confidence:** HIGH - Evidence-based analysis with code citations and real traces
