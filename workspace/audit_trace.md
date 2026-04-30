# REQUEST FLOW TRACE - Understanding True Decision Ownership

## TRACED PROMPTS

### 1. "hi"

**Entry**: `slack_client.py` → `SlackClient._handle_message_event()`
- Calls `bridge.should_send_progress()` → `assistant_layer.decide_without_llm()` 
  - **DECISION**: deterministic pattern match in `_quick_answer_decision()`
  - Matches "hi" in social_messages tuple
  - Returns `RequestMode.ANSWER` immediately
  - **NO "Working on your request..." message**

**Main Flow**: `supervisor.handle_user_goal()`
- Records user message in memory
- Calls `assistant_layer.decide()`:
  - Hits `_quick_answer_decision()` first (same logic)
  - **RETURNS ANSWER MODE WITHOUT LLM**
  
**Response Path**: `assistant_layer.build_answer_response()`
- → `ConversationalHandler.handle()`
- Builds `ConversationContext`:
  ```python
  recent_tasks = self.task_store.list_tasks()[:3]
  runtime_snapshot = self.operator_context.build_runtime_snapshot(focus_text=user_message)
  ```
  - **PROBLEM**: Always fetches recent_tasks and full runtime snapshot
- Calls `_answer_deterministically()`:
  ```python
  if self._is_short_social_message(message, ("hello", "hi", "hey")):
      if runtime.active_tasks:
          return f"Hi. I'm in the middle of {runtime.active_tasks[0]}..."
      return "Hi. I'm Sovereign, and I'm ready to help..."
  ```
  - **PROBLEM**: Hardcoded to check active_tasks even for lightweight greeting

**Verdict**: Python owns the entire interaction. LLM never involved. Active task state contamination is deterministic.

---

### 2. "what do you remember about me?"

**Entry**: Same Slack path

**Decision**: `assistant_layer.decide()`
- Not in quick-answer patterns
- If LLM configured, calls `_decide_with_llm()`:
  - Sends FULL context block via `context_assembler.build('operator', user_message=...)`
  - Context includes: runtime_snapshot, all tools, all agents, all memory layers
  - LLM classifies as ANSWER mode
- Falls back to deterministic if no LLM:
  - Matches "what do you know about me" in answer_markers tuple
  - Returns ANSWER mode

**Response Path**: `ConversationalHandler.handle()`
- Builds full ConversationContext (recent_tasks, runtime_snapshot, etc.)
- Tries `_answer_with_llm()`:
  - Unless `_prefer_local_answer()` says to skip LLM
  - "what do you know about me" is in quick_phrases, so **SKIPS LLM**
- Falls to `_answer_deterministically()`:
  ```python
  if "what do you know about me" in message:
      return self._describe_user_memory(runtime)
  ```
  - Hardcoded response path
  
**Verdict**: Python owns the interaction. LLM bypassed for this question. Response pulled from runtime snapshot.

---

### 3. "remind me in 2 minutes to drink water"

**Entry**: Slack path with progress indicator

**Decision**: `assistant_layer.decide()`
- Not a quick-answer
- If LLM configured, tries LLM classification (might return ACT/SINGLE_ACTION)
- Deterministic path:
  ```python
  if "remind me" in message or "set a reminder" in message:
      return AssistantDecision(
          mode=RequestMode.ACT,
          escalation_level=ExecutionEscalation.SINGLE_ACTION,
          reasoning="Reminder requests should go through live reminder scheduling path."
      )
  ```

**Execution Path**: FULL SUPERVISOR LOOP
1. **Task Creation**: `Task` object with ACT/SINGLE_ACTION
2. **Planning**: `planner.create_plan()`
   - Detects `_looks_like_reminder_goal()` → True
   - Returns `_create_reminder_plan()`:
     ```python
     [
       SubTask(title="Capture reminder context", agent="memory_agent"),
       SubTask(title="Schedule reminder delivery", agent="reminder_scheduler_agent"),
       SubTask(title="Review reminder scheduling evidence", agent="reviewer_agent"),
     ]
     ```
   - **3 subtasks** with sequential dependencies
3. **Execution Loop**: 
   - Iteration 1: memory_agent.run() → captures context
   - Iteration 2: reminder_scheduler_agent.run() → parses + schedules
   - Iteration 3: reviewer_agent.run() → verifies
4. **Evaluation**: `evaluator.evaluate()` checks review evidence
5. **Response Composition**: `assistant_layer.compose_task_response()`
   - Tries `_compose_with_llm()` with serialized task results
   - Falls back to `_compose_deterministically()` with templated response

**Verdict**: This is a 3-agent execution workflow for a simple assistant action. No fast path exists.

---

### 4. "please write me a 24 solver python script and put it in my folder"

**Entry**: Slack with progress indicator

**Decision**: `assistant_layer.decide()`
- LLM path likely returns EXECUTE + BOUNDED_TASK_EXECUTION
- Deterministic path:
  - Matches "write" in action_markers
  - But also checks execute_markers ("implement", "build")
  - Returns EXECUTE if execution language found

**Execution Path**: FULL SUPERVISOR LOOP
1. **Planning**: LLM or deterministic
   - If LLM configured: sends to planning agent with context
   - Creates subtasks (likely coding_agent + reviewer_agent)
2. **Routing**: assigns agents
3. **Execution**: coding_agent creates file
4. **Review**: reviewer_agent verifies
5. **Evaluation**: checks for verified evidence
6. **Response**: LLM or deterministic composition

**Verdict**: Legitimate multi-step execution. This one is appropriate for the full path.

---

## KEY CONTAMINATION POINTS

### A. ConversationContext Always Includes Task State
```python
# conversation.py line 93-102
def _build_context(self, user_message: str) -> ConversationContext:
    recent_tasks = self.task_store.list_tasks()[:3]  # ALWAYS FETCHED
    return ConversationContext(
        recent_tasks=recent_tasks,
        runtime_snapshot=self.operator_context.build_runtime_snapshot(focus_text=user_message),
        # ... more state
    )
```

### B. RuntimeSnapshot Is Too Broad
```python
# operator_context.py line 276-329
def build_runtime_snapshot(self, *, focus_text: str | None = None) -> RuntimeSnapshot:
    # Always includes:
    # - active_tasks
    # - recent_actions
    # - open_loops
    # - pending_reminders
    # - user_memory, project_memory, operational_memory
    # - agent_roles
    # - assistant_recall (with retrieval)
```

### C. Greeting Handler Checks Active Tasks
```python
# conversation.py line 141-144
if self._is_short_social_message(message, ("hello", "hi", "hey")):
    if runtime.active_tasks:
        return f"Hi. I'm in the middle of {runtime.active_tasks[0]}..."
    return "Hi. I'm Sovereign, and I'm ready to help..."
```

### D. Reminder Goes Through Full Execution Loop
No fast path. Always creates Task → plans 3 subtasks → executes → reviews → evaluates.

---

## WHERE PYTHON OVERCONTROLS

### 1. **Quick Answer Bypass** (`assistant.py` lines 160-197)
- Python pattern matching happens BEFORE LLM decision
- Many messages never reach LLM:
  - "hi", "hey", "thanks"
  - Preference statements
  - Simple math
- This is a **deterministic gate** that blocks LLM interpretation

### 2. **Deterministic Decision Fallback** (`assistant.py` lines 199-351)
- Extensive keyword matching for modes:
  - action_markers, execute_markers, conversational_markers
  - Hardcoded phrases like "help me plan", "what can you do"
- This is a **Python-first routing** system

### 3. **Context Assembly Is Mandatory** (`context_assembly.py` lines 137-157)
- Every LLM call gets full context bundle:
  - Instructions for role
  - Runtime snapshot
  - Capability catalog
  - Agent catalog
- **No LLM-driven context filtering**

### 4. **Hardcoded Conversational Responses** (`conversation.py` lines 134-223)
- 50+ hardcoded response paths:
  - "what are you working on" → `_describe_active_work()`
  - "what model are you using" → `_describe_model()`
  - "what tools do you have" → `_describe_tools()`
  - etc.
- **Python templates replace LLM generation**

### 5. **Response Composition Templates** (`assistant.py` lines 442-493)
- `_compose_deterministically()` has templated response logic:
  - If blocked: "I'm blocked on X. To keep going, I need Y."
  - If ACT mode: "I [action]."
  - If EXECUTE mode: "I worked through this and [actions]."
- **Python shapes the narrative structure**

---

## WHERE LLM IS TOO DOWNSTREAM

### 1. **Mode Classification Only** (`assistant.py` lines 91-158)
- LLM receives pre-assembled context and is asked to choose: ANSWER, ACT, or EXECUTE
- This is **classification within Python-defined constraints**, not true interpretation

### 2. **Response Formatting** (`assistant.py` lines 398-440)
- LLM gets serialized task results and is told to "compose final reply"
- Prompt says: "Write the final user-facing reply" not "Interpret the results and respond"
- **LLM is narrating Python's execution summary**

### 3. **Conversational Answering** (`conversation.py` lines 104-132)
- LLM receives pre-built ConversationContext with:
  - Recent tasks (already fetched)
  - Runtime snapshot (already assembled)
  - Workspace entries (already listed)
- Prompt says: "Reply as assistant. Use provided context."
- **LLM is formatting Python's context dump, not deciding what context matters**

### 4. **Planning** (`planner.py` lines 79-139)
- LLM creates subtasks, but within strict bounds:
  - Must return JSON with specific shape
  - Must choose from fixed agent list
  - Must use only supported tool invocations
  - Plan size is constrained by escalation_level (2-5 subtasks)
- **LLM is filling Python's template, not designing the approach**

### 5. **Routing** (`router.py` lines 66-102)
- LLM classification is just "pick an agent from this list"
- Not "determine how to handle this subtask"
- **LLM is filling Python's routing slot**

---

## EVIDENCE: USER DIAGNOSIS IS CORRECT

### The user said:
> "the LLM sounds intelligent, but feels like it is only being passed source/state summaries and told to turn them into sentences"

**THIS IS ACCURATE**. The LLM receives:
- Pre-serialized task results (Python-assembled)
- Pre-built runtime snapshots (Python-assembled)
- Pre-fetched context (Python-decided relevance)

And is instructed to:
- "Write the final reply" (formatting)
- "Return only valid JSON" (structured output for Python consumption)
- "Use the provided context" (no agency over what context matters)

### The user said:
> "the first response does NOT feel like a frontier assistant"

**THIS IS ACCURATE**. First-touch interpretation happens in:
- `_quick_answer_decision()` - deterministic pattern matching
- `_decide_deterministically()` - keyword matching
- LLM only involved if these don't match

### The user said:
> "greetings and memory questions get contaminated by active-task or recent-task state"

**THIS IS ACCURATE**. Evidence:
```python
# conversation.py line 142-143
if runtime.active_tasks:
    return f"Hi. I'm in the middle of {runtime.active_tasks[0]}..."
```

This is **Python overcontrol** - the greeting handler shouldn't default to checking tasks.

### The user said:
> "reminder requests are going through too much machinery"

**THIS IS ACCURATE**. Reminder path:
1. Task creation
2. Plan 3 subtasks
3. Execute memory_agent
4. Execute reminder_scheduler_agent  
5. Execute reviewer_agent
6. Evaluate with reviewer evidence
7. Compose response

This is a **full execution workflow** for what should be a simple assistant action.

---

## CONCLUSION

**The system is "Python decides, LLM narrates" NOT "LLM decides, Python executes".**

First-touch ownership: **Python** (via `_quick_answer_decision()` and `_decide_deterministically()`)

Context assembly: **Python** (via `ContextAssembler` and `build_runtime_snapshot()`)

Response shaping: **Python** (via hardcoded templates and deterministic helpers)

LLM role: **Formatting and classification** (within Python-defined bounds)
