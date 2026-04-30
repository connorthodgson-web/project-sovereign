# PROJECT SOVEREIGN: FRONT-DOOR OWNERSHIP AUDIT

**Date**: 2026-04-21  
**Scope**: Architectural audit of interaction ownership and LLM-first vs Python-first decision-making  
**Goal**: Determine why the system feels like "Python decides, LLM narrates" and design minimum refactor to fix it

---

## 1. EXECUTIVE VERDICT

### Is the user's diagnosis correct?

**YES. The diagnosis is accurate.**

The current system is fundamentally "Python decides, LLM narrates" rather than "LLM decides, Python executes."

### Is the problem architectural?

**YES, but it's fixable without a rewrite.**

The problem is structural but not foundational. The supervisor/planner/router/agent architecture is sound. The issue is that **interaction ownership lives in the wrong layer**.

Decision-making currently happens in:
1. **Deterministic pattern matching first** (`assistant.py` lines 160-197, 199-351)
2. **LLM classification second** (within Python-defined constraints)
3. **Python context assembly always** (before LLM sees anything)

The system needs:
1. **LLM interpretation first** (with thin, relevant context)
2. **LLM-driven context needs** (what context actually matters?)
3. **Python execution underneath** (when LLM determines it's needed)

### Severity

**HIGH IMPACT on assistant feel, MEDIUM complexity to fix.**

This affects every first-touch interaction:
- Greetings sound robotic
- Memory questions get contaminated by task state
- Simple actions go through heavy machinery
- The assistant feels like a workflow logger, not an operator

However, the fix is **targeted architectural surgery**, not a rewrite.

---

## 2. CURRENT FRONT-DOOR OWNERSHIP

### Who owns the first interpretation?

**Python deterministic logic owns the first interpretation.**

**Entry flow**:
```
User message
  → slack_client.py: SlackOperatorBridge.handle_user_message()
    → supervisor.py: Supervisor.handle_user_goal()
      → operator_context.py: record_user_message() [memory capture]
      → assistant.py: AssistantLayer.decide()
        ┌─→ _quick_answer_decision() [PYTHON, runs FIRST]
        │     - Pattern matches: "hi", "thanks", preference statements, simple math
        │     - Returns ANSWER mode immediately
        │     - BLOCKS LLM from seeing these messages
        │
        ├─→ _decide_with_llm() [LLM, runs SECOND, only if quick-answer missed]
        │     - Sends FULL context block (runtime snapshot, tools, agents, memory)
        │     - Asks LLM to classify: ANSWER, ACT, or EXECUTE
        │     - LLM returns JSON with mode/escalation/reasoning
        │     - Falls back to deterministic if LLM unavailable or fails
        │
        └─→ _decide_deterministically() [PYTHON, runs THIRD, fallback]
              - Keyword matching: action_markers, execute_markers, answer_markers
              - Hardcoded phrase matching: "remind me", "what can you do", etc.
              - Returns mode based on Python pattern matching
```

**Verdict**: **Python owns the first decision.** The LLM only sees requests that survive Python's pattern matching.

### Where does ownership shift?

**Ownership shifts depending on the mode:**

**ANSWER mode** (conversational):
```
assistant.py: build_answer_response()
  → conversation.py: ConversationalHandler.handle()
    → _build_context() [PYTHON: fetches recent_tasks, runtime_snapshot]
    → _answer_with_llm() [LLM: formats response from Python context]
    → _answer_deterministically() [PYTHON: 50+ hardcoded response paths]
```
**Verdict**: Python assembles context, LLM formats it (if used at all).

**ACT/EXECUTE mode** (execution):
```
supervisor.py: full execution loop
  → planner.py: create_plan()
    → _create_llm_plan() [LLM: generates subtasks within Python bounds]
    → _create_fallback_plan() [PYTHON: deterministic subtask templates]
  → router.py: route_subtask()
    → _classify_with_llm() [LLM: picks agent from Python list]
    → _classify_deterministically() [PYTHON: keyword-based routing]
  → agents/*.py: agent.run() [PYTHON: tool invocation]
  → evaluator.py: evaluate()
    → _evaluate_with_llm() [LLM: judges completion from Python evidence]
    → _evaluate_deterministically() [PYTHON: evidence-based rules]
  → assistant.py: compose_task_response()
    → _compose_with_llm() [LLM: narrates Python's task summary]
    → _compose_deterministically() [PYTHON: templated responses]
```
**Verdict**: Python orchestrates, LLM fills slots and narrates results.

---

## 3. WHERE PYTHON OVERCONTROLS THE UX

Ranked by **damage to assistant feel**:

### RANK 1: Quick-Answer Bypass (HIGH DAMAGE)
**File**: `core/assistant.py` lines 160-197  
**Function**: `_quick_answer_decision()`

**Problem**: Deterministic pattern matching runs BEFORE the LLM sees the message.

**Impact**:
- "hi", "hey", "thanks" → instantly return ANSWER mode
- Preference statements → instantly return ANSWER mode
- Simple math → instantly return ANSWER mode

**Why this is bad**:
- The LLM never gets a chance to interpret these messages contextually
- The system can't learn that "hi" after a blocker should feel different than "hi" on a fresh start
- The first touch feels rule-based, not intelligent

**Evidence**:
```python
# assistant.py lines 160-182
def _quick_answer_decision(self, user_message: str) -> AssistantDecision | None:
    message = user_message.lower().strip()
    normalized = self._normalize_phrase_text(message)
    social_messages = (
        "hello", "hi", "hey", "yo", "sup", "thanks", "thank you", "help", ...
    )
    if self._is_short_social_message(normalized, social_messages):
        return AssistantDecision(
            mode=RequestMode.ANSWER,
            escalation_level=ExecutionEscalation.CONVERSATIONAL_ADVICE,
            reasoning="This is a lightweight conversational request.",
            should_use_tools=False,
        )
```

This bypasses the LLM entirely.

---

### RANK 2: Hardcoded Conversational Responses (HIGH DAMAGE)
**File**: `core/conversation.py` lines 134-223  
**Function**: `_answer_deterministically()`

**Problem**: 50+ hardcoded response paths for common questions.

**Impact**:
- "what model are you using" → Python template
- "what tools do you have" → Python template
- "what can you do" → Python template
- "what are you working on" → Python template
- etc.

**Why this is bad**:
- The assistant sounds like a FAQ bot, not an intelligent operator
- Responses are rigid and cannot adapt to conversational context
- The LLM's language capabilities are unused

**Evidence**:
```python
# conversation.py lines 149-223
if "what are you working on" in message:
    return self._describe_active_work(runtime)
if "what were we focused on before" in message:
    return self._describe_continuity(runtime)
if "what model are you using" in message:
    return self._describe_model(runtime)
if "what tools do you have" in message:
    return self._describe_tools(runtime)
# ... 40 more hardcoded paths
```

These should be LLM-generated responses using runtime context as input, not Python templates.

---

### RANK 3: Greeting Handler Active-Task Contamination (HIGH DAMAGE)
**File**: `core/conversation.py` lines 141-144  
**Function**: `_answer_deterministically()` → greeting path

**Problem**: Greetings ALWAYS check active_tasks and inject that into the response.

**Impact**:
- User says "hi" → system responds "Hi. I'm in the middle of X..."
- This feels robotic and presumptive
- The user might just be saying hi, not asking about active work

**Why this is bad**:
- The greeting path should be clean by default
- Active task state should only surface if contextually relevant
- This is a Python-driven decision about what context matters, not LLM-driven

**Evidence**:
```python
# conversation.py lines 141-144
if self._is_short_social_message(message, ("hello", "hi", "hey")):
    if runtime.active_tasks:
        return f"Hi. I'm in the middle of {runtime.active_tasks[0]}. If you want, I can keep going or switch context."
    return "Hi. I'm Sovereign, and I'm ready to help. What do you want to tackle?"
```

This is **Python deciding** that active tasks should contaminate greetings.

---

### RANK 4: Mandatory Context Assembly (MEDIUM-HIGH DAMAGE)
**File**: `core/context_assembly.py` lines 137-157  
**File**: `core/operator_context.py` lines 276-329  
**Function**: `ContextAssembler.build()` → `build_runtime_snapshot()`

**Problem**: Every LLM call gets a massive context bundle:
- Runtime model, LLM readiness
- Live tools, scaffolded tools, configured tools, planned tools
- Agent roles
- Active tasks, recent actions, open loops
- Pending reminders, delivered reminders, failed reminders
- User memory, project memory, operational memory
- Assistant recall (retrieved facts)

**Impact**:
- The LLM is bombarded with context whether it needs it or not
- "hi" gets the same context dump as "build me an app"
- No distinction between lightweight and heavyweight requests

**Why this is bad**:
- Context assembly should be **LLM-driven** ("what context do I need?")
- Not **Python-driven** ("here's everything I have")
- This bloats prompts and makes lightweight requests feel heavy

**Evidence**:
```python
# context_assembly.py lines 145-157
def build(...) -> PromptContextBundle:
    instruction_paths = self.role_instruction_files.get(role, ...)
    instruction_text = self.prompt_library.read_many(instruction_paths)
    focus_text = user_message or goal
    return PromptContextBundle(
        role=role,
        instruction_text=instruction_text,
        runtime_snapshot=self.operator_context.build_runtime_snapshot(focus_text=focus_text),  # ALWAYS FULL
        capability_catalog=self.capability_catalog,  # ALWAYS FULL
        agent_catalog=self.agent_catalog,  # ALWAYS FULL
        ...
    )
```

This is **Python deciding** what context the LLM gets.

---

### RANK 5: Reminder Request Full Execution Loop (MEDIUM DAMAGE)
**File**: `core/planner.py` lines 201-228  
**Function**: `_create_reminder_plan()`

**Problem**: Reminder requests go through a 3-subtask execution loop:
1. memory_agent: capture context
2. reminder_scheduler_agent: parse and schedule
3. reviewer_agent: verify scheduling

**Impact**:
- Simple assistant actions feel like multi-step workflows
- Latency is higher than necessary
- The user experiences "Working on your request..." for "remind me in 5 mins"

**Why this is bad**:
- Reminders should be **fast path** assistant actions
- They don't need multi-agent coordination
- The supervisor loop is overkill for this

**Evidence**:
```python
# planner.py lines 201-228
def _create_reminder_plan(self, goal: str, *, escalation_level: ExecutionEscalation) -> list[SubTask]:
    return self._link_dependencies([
        SubTask(title="Capture reminder context", assigned_agent="memory_agent"),
        SubTask(title="Schedule reminder delivery", assigned_agent="reminder_scheduler_agent"),
        SubTask(title="Review reminder scheduling evidence", assigned_agent="reviewer_agent"),
    ])
```

This is **Python deciding** that reminders need 3-agent workflows.

---

### RANK 6: Deterministic Mode Decision Fallback (MEDIUM DAMAGE)
**File**: `core/assistant.py` lines 199-351  
**Function**: `_decide_deterministically()`

**Problem**: Extensive keyword matching for mode classification:
- action_markers: "create", "write", "update", "run", etc.
- execute_markers: "build", "implement", "research", etc.
- answer_markers: "what are you", "what tools do you have", etc.

**Impact**:
- Mode classification feels brittle
- The LLM's contextual understanding is bypassed
- Edge cases get misrouted

**Why this is bad**:
- Mode classification should be LLM-first, Python fallback
- Not Python-first, LLM bypass
- This makes the system feel rule-based

**Evidence**:
```python
# assistant.py lines 199-351
def _decide_deterministically(self, user_message: str) -> AssistantDecision:
    message = user_message.lower()
    normalized = self._normalize_phrase_text(message)
    action_markers = ("create", "write", "update", "edit", "change", "run", "generate", "read", "make", "set", "add")
    execute_markers = ("build", "implement", "plan", "coordinate", "investigate", "research", "review", "audit", "workflow", "multi-step", "end-to-end")
    # ... 150 lines of keyword matching
```

This is **Python routing** instead of LLM interpretation.

---

### RANK 7: Response Composition Templates (MEDIUM DAMAGE)
**File**: `core/assistant.py` lines 442-493  
**Function**: `_compose_deterministically()`

**Problem**: Python templates shape the narrative structure of task responses.

**Impact**:
- Responses sound formulaic
- "I worked through this and created X, ran Y, and verified Z."
- "I'm blocked on X. To keep going, I need Y."

**Why this is bad**:
- The LLM should own the narrative, not fill Python templates
- Response tone and structure should vary with context

**Evidence**:
```python
# assistant.py lines 460-493
if task.status == TaskStatus.BLOCKED and blocked_result is not None:
    completed_prefix = ""
    if meaningful_actions:
        completed_prefix = f"I already {self._join_phrases(meaningful_actions[:3])}. "
    blocker = self._describe_blocker(blocked_result)
    next_step = self._describe_next_step(blocked_result)
    return f"{completed_prefix}I'm blocked on {blocker}. {next_step}".strip()

if decision.mode == RequestMode.ACT:
    if meaningful_actions:
        return f"I {self._join_phrases(meaningful_actions[:3])}."
    return "I handled that."
```

This is **Python shaping** the response structure.

---

## 4. WHERE THE LLM IS TOO DOWNSTREAM

### A. LLM Receives Pre-Assembled Context

**Problem**: By the time the LLM sees a request, Python has already:
1. Decided what context is relevant (`build_runtime_snapshot()`)
2. Assembled the full context bundle (`ContextAssembler.build()`)
3. Serialized it into prompt format

**What the LLM should do**: Determine what context it needs
**What the LLM actually does**: Receive what Python decided to give it

**Code evidence**:
```python
# assistant.py lines 95-124
prompt = (
    f"{self.context_assembler.build('operator', user_message=user_message).to_prompt_block()}\n"
    "Classify how the CEO assistant should handle the user's request.\n"
    # ...
)
```

The LLM gets `context_assembler.build()` output, which is Python-decided.

---

### B. LLM Classifies Within Python-Defined Constraints

**Problem**: When the LLM is involved in mode classification, it's choosing from Python-defined options:
- Mode: ANSWER, ACT, or EXECUTE (Python-defined enum)
- Escalation: conversational_advice, single_action, bounded_task_execution, objective_completion (Python-defined enum)
- Must return JSON with specific shape (Python-defined structure)

**What the LLM should do**: Interpret the request and determine how to handle it
**What the LLM actually does**: Fill Python's classification template

**Code evidence**:
```python
# assistant.py lines 95-99
prompt = (
    # ...
    "Choose exactly one mode and one escalation level.\n"
    "Modes:\n"
    "- ANSWER: direct conversational reply, no execution loop\n"
    "- ACT: one small action or simple tool use\n"
    "- EXECUTE: task or objective that needs planning/execution/review\n"
)
```

The LLM is **classifying**, not **interpreting**.

---

### C. LLM Formats Pre-Serialized Task Results

**Problem**: When composing task responses, the LLM receives:
1. Serialized task results (Python-assembled)
2. Outcome summary (Python-computed)
3. Evaluation result (Python or LLM-evaluated)

And is asked to "write the final user-facing reply."

**What the LLM should do**: Own the response based on raw evidence
**What the LLM actually does**: Narrate Python's summary

**Code evidence**:
```python
# assistant.py lines 409-427
prompt = (
    f"{self.context_assembler.build('operator', goal=task.goal).to_prompt_block()}\n"
    "Write the final user-facing reply for Project Sovereign's CEO assistant.\n"
    # ...
    f"User goal: {task.goal}\n"
    f"Outcome: {outcome.model_dump()}\n"
    f"Evaluation: {evaluation.model_dump()}\n"
    f"Structured results: {json.dumps(self._serialize_results(task.results), ensure_ascii=True)}"
)
```

The LLM is **formatting** Python's results, not **interpreting** them.

---

### D. LLM Generates Subtasks Within Python Bounds

**Problem**: When the LLM plans subtasks, it must:
- Return JSON with specific shape
- Choose agents from a fixed list
- Use only supported tool invocations
- Stay within subtask count bounds (2-5 subtasks depending on escalation)

**What the LLM should do**: Design the execution approach
**What the LLM actually does**: Fill Python's subtask template

**Code evidence**:
```python
# planner.py lines 88-105
prompt = (
    # ...
    "Return strict JSON with the shape "
    '{"subtasks":[{"title":"...","description":"...","objective":"...","agent_hint":"...",'
    '"tool_invocation":{"tool_name":"file_tool","action":"write","parameters":{"path":"...","content":"..."}}|null}]}.'
    "\n"
    "Only use these agent_hint values: coding_agent, browser_agent, research_agent, "
    "memory_agent, reviewer_agent, communications_agent, reminder_scheduler_agent.\n"
    'Only use tool_invocation for supported file_tool actions write, read, list or runtime_tool action run.\n'
    # ...
    "Plan sizing guidance:\n"
    "- single_action: 2 to 3 subtasks max, minimal scaffolding\n"
    "- bounded_task_execution: 3 to 4 subtasks, contained execution\n"
    "- objective_completion: 4 to 5 subtasks, include explicit review/adapt coverage\n"
)
```

The LLM is **filling slots**, not **designing**.

---

### E. LLM Routes by Picking Agents

**Problem**: When routing subtasks, the LLM is asked to "Choose the best agent from this fixed set."

**What the LLM should do**: Determine how to handle the subtask
**What the LLM actually does**: Pick an agent from Python's list

**Code evidence**:
```python
# router.py lines 70-76
prompt = (
    f"{self.context_assembler.build('router', goal=subtask.objective).to_prompt_block()}\n"
    "Choose the best agent for the subtask from this fixed set only:\n"
    f"{', '.join(self.available_agents())}\n"
    "Return strict JSON with the shape "
    '{"agent_name":"coding_agent","reasoning":"..."}.'
)
```

The LLM is **picking**, not **interpreting**.

---

### F. Summary: LLM Is Boxed Into Formatting

**The pattern is consistent across all LLM invocations**:
1. Python assembles context
2. Python defines the structure
3. LLM fills the structure
4. Python consumes the structured output

**The LLM is never given**:
- Agency over what context it needs
- Freedom to design the approach
- Ownership of the response

**The LLM is always**:
- Given pre-assembled context
- Asked to return structured JSON
- Used for formatting/narration

---

## 5. CONTEXT CONTAMINATION FINDINGS

### A. Why Greetings Get Polluted

**Root cause**: Hardcoded logic in `conversation.py` line 142-143:
```python
if self._is_short_social_message(message, ("hello", "hi", "hey")):
    if runtime.active_tasks:
        return f"Hi. I'm in the middle of {runtime.active_tasks[0]}..."
```

**Problem**: Python deterministically injects active_tasks into greetings.

**Fix**: Remove this hardcoded injection. Let the LLM decide whether active context matters for this greeting.

---

### B. Why Memory Questions Get Polluted

**Root cause**: `ConversationContext` always includes recent_tasks:
```python
# conversation.py lines 93-102
def _build_context(self, user_message: str) -> ConversationContext:
    recent_tasks = self.task_store.list_tasks()[:3]  # ALWAYS FETCHED
    return ConversationContext(
        recent_tasks=recent_tasks,
        recent_created_files=self._recent_created_files(recent_tasks),
        workspace_entries=self._workspace_entries(),
        runtime_snapshot=self.operator_context.build_runtime_snapshot(focus_text=user_message),
        # ...
    )
```

**Problem**: Task state is fetched regardless of whether it's relevant to the user's question.

**Fix**: Make context assembly conditional. Only fetch task state if the message suggests task-related intent.

---

### C. What Should Be Filtered

**Filter by message intent**:
- **Greetings** ("hi", "hey") → NO task state by default
- **Meta questions** ("what can you do?") → NO task state, YES capability state
- **Memory questions** ("what do you remember?") → YES memory, NO task state unless asked
- **Status questions** ("what are you working on?") → YES task state
- **Continuation** ("keep going", "continue") → YES task state
- **Execution requests** → YES full context

**Implement a relevance classifier**:
```python
def classify_context_needs(message: str) -> ContextNeeds:
    """Determine what context is actually relevant for this message."""
    # Could be LLM-driven or heuristic-based
    # Returns: ContextNeeds(needs_task_state, needs_memory, needs_capabilities, etc.)
```

---

### D. What Should Stay

**Always include**:
- System identity (who the assistant is)
- LLM readiness status (whether external reasoning is available)

**Conditionally include**:
- Task state (only if message suggests task-related intent)
- Memory (only if message suggests memory-related intent)
- Capabilities (only if message suggests capability-related intent)
- Open loops (only if message suggests continuation intent)

---

## 6. SIMPLE ACTION VS EXECUTION TASK DESIGN

### Current Problem

**The system treats all non-ANSWER requests as execution tasks**:
- Reminder requests → 3-subtask loop
- File creation → 2-3 subtask loop with memory + coding + review
- Small actions → full supervisor iteration

**There is no fast path for simple assistant actions.**

---

### How These Should Differ

#### **Simple Assistant Actions**
Examples: reminders, quick answers, status checks, preference settings

**Should feel like**:
- Instant or near-instant
- Direct LLM response → tool call → confirmation
- No multi-agent coordination
- No heavy scaffolding

**Should NOT**:
- Create Task objects
- Go through planner/router/evaluator
- Have reviewer verification
- Involve multiple agents

**Proposed path**:
```
User message
  → LLM interprets: "This is a simple reminder request"
  → LLM generates: reminder summary, delivery time
  → Direct tool call: reminder_scheduler.schedule()
  → LLM confirms: "Scheduled. I'll remind you at 3:15 PM."
```

**Key differences**:
- LLM owns interpretation and response
- Tool execution is direct, not orchestrated
- No task state, no subtasks, no agents

---

#### **Execution Tasks**
Examples: build an app, research and summarize, multi-step workflows

**Should feel like**:
- Planned and deliberate
- Progress visibility
- Multi-agent coordination when needed
- Evidence and review

**Should**:
- Create Task objects
- Go through planner/router/evaluator
- Have reviewer verification for quality
- Involve multiple agents

**Current path** (keep this):
```
User message
  → LLM interprets: "This needs planning and execution"
  → Create Task
  → Planner creates subtasks
  → Router assigns agents
  → Agents execute
  → Reviewer verifies
  → Evaluator judges completion
  → LLM composes response
```

**Key differences**:
- Supervisor orchestrates
- Task state is durable
- Evidence and review are required
- Multiple agents coordinate

---

### Concrete Reminder Design

**Problem**: Reminders currently go through:
1. AssistantLayer.decide() → ACT + SINGLE_ACTION
2. Task creation
3. Planner: 3 subtasks (memory → reminder → reviewer)
4. Router: assign agents
5. Execute memory_agent
6. Execute reminder_scheduler_agent
7. Execute reviewer_agent
8. Evaluator judges completion
9. Compose response

**This is WAY too heavy.**

**Proposed fast path**:
```
User: "remind me in 10 mins to check the deployment"

LLM first-pass interpretation:
  - Intent: schedule reminder
  - Summary: "check the deployment"
  - Time: 10 minutes from now
  - Needs: reminder_scheduler tool

Direct tool invocation:
  reminder_scheduler.schedule(
    summary="check the deployment",
    deliver_at=now + 10 minutes,
    channel=interaction.channel_id
  )

LLM response composition:
  "I'll remind you at 3:45 PM to check the deployment."
```

**Key changes**:
- No Task object
- No planner/router/evaluator
- LLM interprets → tool executes → LLM confirms
- Fast, lightweight, assistant-like

---

### How to Keep Simple Actions Fast and Natural

**Principles**:
1. **LLM owns the interpretation** - not Python pattern matching
2. **Direct tool invocation** - not orchestrated execution
3. **No supervisor loop** - unless genuinely needed
4. **Natural response** - not templated confirmation

**Implementation**:
- Add a `FastActionLayer` that sits before the supervisor
- LLM decides: "This is a fast action" vs "This needs execution"
- Fast actions bypass Task creation and go straight to tool invocation
- Execution tasks go through the current supervisor loop

---

## 7. MINIMUM HIGH-LEVERAGE REFACTOR

### Core Idea: **Make the LLM the Front Door**

**Current flow**:
```
User message
  → Python pattern matching (_quick_answer_decision)
  → Python context assembly (build_runtime_snapshot)
  → LLM classification (within Python bounds)
  → Python orchestration (supervisor/planner/router)
  → LLM narration (_compose_with_llm)
```

**Target flow**:
```
User message
  → LLM first-pass interpretation (thin context)
  → LLM determines context needs
  → LLM decides: fast action vs execution task
  → Python executes (tools or supervisor loop)
  → LLM composes response (not just narration)
```

---

### Concrete Changes

#### **Change 1: Remove Quick-Answer Bypass**

**Current**: `assistant.py` runs `_quick_answer_decision()` BEFORE LLM

**Target**: LLM sees every message first (with thin context)

**Implementation**:
```python
# assistant.py
def decide(self, user_message: str) -> AssistantDecision:
    # REMOVE: quick_decision = self._quick_answer_decision(user_message)
    
    # NEW: LLM first-pass with THIN context
    llm_decision = self._decide_with_llm_first(user_message)
    if llm_decision is not None:
        return llm_decision
    
    # Fallback to deterministic if LLM unavailable
    return self._decide_deterministically(user_message)
```

**Impact**: LLM owns first interpretation for all messages.

---

#### **Change 2: Thin Context for LLM First-Pass**

**Current**: `_decide_with_llm()` sends full context bundle

**Target**: Send only essential context for interpretation

**Implementation**:
```python
def _decide_with_llm_first(self, user_message: str) -> AssistantDecision | None:
    # Build THIN context: identity + recent conversation only
    thin_context = self._build_thin_context(user_message)
    
    prompt = (
        f"{thin_context}\n"
        "Interpret this message and decide how to handle it.\n"
        "You can:\n"
        "1. Answer conversationally (no tools needed)\n"
        "2. Perform a quick action (reminder, file op, status check)\n"
        "3. Execute a task (planning, multi-step, coordination)\n"
        "Return: {\"mode\":\"answer|quick_action|execute_task\",\"reasoning\":\"...\",\"context_needs\":[\"task_state\",\"memory\",\"capabilities\"]}\n"
        f"Message: {user_message}"
    )
    # ... LLM call
```

**Key difference**: LLM decides what context it needs, not Python.

---

#### **Change 3: LLM-Driven Context Assembly**

**Current**: Python always assembles full context

**Target**: LLM requests specific context based on message intent

**Implementation**:
```python
def _assemble_context_for_decision(
    self, 
    user_message: str, 
    context_needs: list[str]
) -> dict[str, Any]:
    """Assemble only the context the LLM requested."""
    context = {"message": user_message}
    
    if "task_state" in context_needs:
        context["active_tasks"] = self._get_active_tasks()
    if "memory" in context_needs:
        context["relevant_memory"] = self._get_relevant_memory(user_message)
    if "capabilities" in context_needs:
        context["live_capabilities"] = self._get_live_capabilities()
    # ... etc
    
    return context
```

**Key difference**: Context is assembled based on LLM's request, not Python's assumption.

---

#### **Change 4: Add Fast Action Path**

**Current**: All non-ANSWER requests go through supervisor loop

**Target**: Quick actions bypass supervisor and go straight to tool invocation

**Implementation**:
```python
# New file: core/fast_actions.py
class FastActionHandler:
    def can_handle_fast(self, decision: AssistantDecision) -> bool:
        """Check if this can be handled as a fast action."""
        return decision.mode == RequestMode.QUICK_ACTION
    
    def handle_fast(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
        """Handle quick actions without supervisor loop."""
        # LLM interprets the action parameters
        action_spec = self._extract_action_with_llm(user_message, decision)
        
        # Direct tool invocation
        result = self._execute_tool_direct(action_spec)
        
        # LLM composes confirmation
        response_text = self._compose_response_with_llm(user_message, action_spec, result)
        
        return ChatResponse(...)
```

**Integration**:
```python
# supervisor.py
def handle_user_goal(self, goal: str) -> ChatResponse:
    decision = self.assistant_layer.decide(goal)
    
    if decision.mode == RequestMode.ANSWER:
        return self.assistant_layer.build_answer_response(goal, decision)
    
    # NEW: Fast action path
    if self.fast_action_handler.can_handle_fast(decision):
        return self.fast_action_handler.handle_fast(goal, decision)
    
    # Existing execution path for tasks
    task = Task(...)
    # ... existing supervisor loop
```

**Impact**: Simple actions feel instant and lightweight.

---

#### **Change 5: Remove Hardcoded Conversational Responses**

**Current**: `conversation.py` has 50+ hardcoded response paths

**Target**: LLM generates responses dynamically

**Implementation**:
```python
def handle(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
    # REMOVE: Extensive _answer_deterministically() paths
    
    # NEW: Always use LLM for conversational responses
    context = self._build_relevant_context(user_message, decision)
    reply = self._answer_with_llm(user_message, context)
    
    # Fallback only if LLM unavailable
    if reply is None:
        reply = self._minimal_fallback(user_message)
    
    return ChatResponse(...)
```

**Impact**: Conversational responses feel intelligent, not robotic.

---

#### **Change 6: Remove Greeting Active-Task Injection**

**Current**: Greetings always check active_tasks

**Target**: LLM decides whether to mention active work

**Implementation**:
```python
# conversation.py - REMOVE these lines:
# if runtime.active_tasks:
#     return f"Hi. I'm in the middle of {runtime.active_tasks[0]}..."

# NEW: Let LLM decide
# If LLM determines active_tasks is relevant context, it will mention it
# If not, it won't
```

**Impact**: Greetings feel natural, not contaminated.

---

### Why This Is Better Than Local Polish Only

**Local polish** would be:
- Remove the greeting injection
- Add a reminder fast path
- Tweak a few hardcoded responses

**This refactor** is:
- **Structural ownership shift**: LLM becomes the front door
- **Architectural simplification**: Remove Python's interpretation layer
- **Systematic fix**: All messages get LLM-first treatment

**The difference**:
- Local polish: band-aids on symptoms
- This refactor: fixes the underlying ownership problem

**Effort**:
- Local polish: 5-10 targeted fixes, still feels Python-led
- This refactor: 4-6 core changes, fundamentally shifts ownership to LLM

**Impact**:
- Local polish: Marginal improvement
- This refactor: System feels LLM-led

---

## 8. EXACT CODE TARGETS

Listed in **implementation order**:

### Phase 1: LLM First-Pass (Foundation)

#### **File 1: `core/assistant.py`**
**Lines to change**: 59-66, 91-158, 160-197
**Function**: `decide()`, `_decide_with_llm()`, `_quick_answer_decision()`

**Changes**:
1. Remove `_quick_answer_decision()` call from `decide()`
2. Create new `_decide_with_llm_first()` that sends thin context
3. Make LLM request context needs, not receive pre-assembled bundle
4. Update `_decide_with_llm()` to use thin context

**New behavior**: LLM sees every message with minimal context first.

---

#### **File 2: `core/context_assembly.py`**
**Lines to change**: 137-157
**Function**: `build()`

**Changes**:
1. Add `context_needs` parameter to `build()`
2. Make `build_runtime_snapshot()` conditional:
   - If `context_needs` includes "task_state", fetch it
   - If not, skip it
3. Create `build_thin()` method for first-pass interpretation

**New behavior**: Context assembly is driven by LLM's needs.

---

#### **File 3: `core/operator_context.py`**
**Lines to change**: 276-329
**Function**: `build_runtime_snapshot()`

**Changes**:
1. Add optional `include_*` flags for selective assembly:
   - `include_task_state`, `include_memory`, `include_capabilities`, etc.
2. Short-circuit expensive operations when flags are False

**New behavior**: Runtime snapshot is lightweight by default.

---

### Phase 2: Fast Action Path

#### **File 4: NEW `core/fast_actions.py`**
**Lines**: NEW FILE (~200 lines)

**Create**:
```python
class FastActionHandler:
    def can_handle_fast(self, decision: AssistantDecision) -> bool:
        """Check if this is a quick action."""
    
    def handle_fast(self, user_message: str, decision: AssistantDecision) -> ChatResponse:
        """Handle quick actions directly without supervisor loop."""
        # LLM extracts action parameters
        # Direct tool invocation
        # LLM composes confirmation
```

**New behavior**: Quick actions bypass supervisor.

---

#### **File 5: `core/supervisor.py`**
**Lines to change**: 49-55
**Function**: `handle_user_goal()`

**Changes**:
1. Add FastActionHandler initialization
2. After `decide()`, check if fast action path applies
3. Route to `fast_action_handler.handle_fast()` if yes

**New behavior**: Supervisor delegates fast actions.

---

### Phase 3: Remove Python Overcontrol

#### **File 6: `core/conversation.py`**
**Lines to change**: 93-102, 104-132, 134-223
**Functions**: `_build_context()`, `_answer_with_llm()`, `_answer_deterministically()`

**Changes**:
1. Make `_build_context()` conditional based on message intent
2. Always prefer `_answer_with_llm()`, not deterministic templates
3. Simplify `_answer_deterministically()` to ~10 essential fallbacks only
4. **REMOVE**: greeting active-task injection (line 142-143)
5. **REMOVE**: 40+ hardcoded response paths

**New behavior**: LLM generates responses, Python only falls back when LLM unavailable.

---

#### **File 7: `core/assistant.py`**
**Lines to change**: 199-351, 442-493
**Functions**: `_decide_deterministically()`, `_compose_deterministically()`

**Changes**:
1. Simplify `_decide_deterministically()` to handle only:
   - Obvious execution markers ("build", "implement")
   - Obvious reminder markers ("remind me")
   - Safety fallback (default to ANSWER mode)
2. Simplify `_compose_deterministically()` to basic templates only

**New behavior**: Deterministic logic is minimal safety net, not primary path.

---

### Phase 4: Planning and Routing Improvements

#### **File 8: `core/planner.py`**
**Lines to change**: 56-77, 201-228
**Functions**: `create_plan()`, `_create_reminder_plan()`

**Changes**:
1. **REMOVE**: `_create_reminder_plan()` (reminders now use fast path)
2. Update `create_plan()` to skip reminder special case
3. Make LLM planning less constrained (looser bounds)

**New behavior**: Planning focuses on genuine execution tasks.

---

#### **File 9: `core/router.py`**
**Lines to change**: 66-102
**Functions**: `_classify_with_llm()`, `_classify_deterministically()`

**Changes**:
1. Give LLM more freedom in routing (less strict bounds)
2. Simplify deterministic routing to safety fallback only

**New behavior**: Routing is LLM-driven with minimal Python override.

---

## 9. IMPLEMENTATION STRATEGY

### Sequenced Plan (Phased)

#### **Phase 1: LLM First-Pass Foundation (Week 1)**
**Goal**: Make LLM the front door for all requests.

**Steps**:
1. Create `_build_thin_context()` in `assistant.py`
2. Create `_decide_with_llm_first()` in `assistant.py`
3. Update `decide()` to call `_decide_with_llm_first()` BEFORE deterministic
4. Add `context_needs` to `ContextAssembler.build()`
5. Add conditional flags to `build_runtime_snapshot()`
6. **Test**: Verify all messages hit LLM first
7. **Test**: Verify thin context reduces prompt size

**Success criteria**:
- LLM sees every message first
- Context is minimal for first-pass interpretation
- Deterministic logic only runs as fallback

**Risk mitigation**:
- Keep deterministic fallback unchanged initially
- Feature flag: `ENABLE_LLM_FIRST_PASS` (default: True)

---

#### **Phase 2: Fast Action Path (Week 2)**
**Goal**: Create lightweight path for simple assistant actions.

**Steps**:
1. Create `core/fast_actions.py` with `FastActionHandler`
2. Define fast action extraction (LLM parses action parameters)
3. Implement direct tool invocation path
4. Update `supervisor.py` to route fast actions
5. **Test**: "remind me in 5 mins" bypasses supervisor
6. **Test**: Response latency < 2 seconds for fast actions

**Success criteria**:
- Reminder requests feel instant
- No Task objects created for fast actions
- Response is natural, not templated

**Risk mitigation**:
- Start with reminders only
- Expand to other fast actions incrementally
- Keep supervisor path as fallback for complex reminders

---

#### **Phase 3: Remove Python Overcontrol (Week 3)**
**Goal**: Let LLM own conversational responses.

**Steps**:
1. Remove greeting active-task injection in `conversation.py`
2. Simplify `_answer_deterministically()` to ~10 essential fallbacks
3. Always call `_answer_with_llm()` when LLM available
4. Simplify `_decide_deterministically()` to safety net only
5. Simplify `_compose_deterministically()` to basic templates
6. **Test**: Greetings feel clean
7. **Test**: Memory questions don't leak task state
8. **Test**: Responses sound intelligent, not robotic

**Success criteria**:
- Greetings: "Hi." gets "Hi. What can I help with?" not "Hi. I'm in the middle of X."
- Memory questions: clean answers, no task contamination
- Responses: LLM-generated, not Python templates

**Risk mitigation**:
- Keep deterministic fallbacks for LLM unavailable case
- Feature flag: `PREFER_LLM_RESPONSES` (default: True)

---

#### **Phase 4: Testing and Refinement (Week 4)**
**Goal**: Validate the refactor and tune behavior.

**Steps**:
1. Run test suite for all example prompts
2. Manual testing: greetings, memory, reminders, tasks
3. Tune LLM prompts for optimal behavior
4. Add telemetry: track which path each request takes
5. A/B test with/without refactor (if possible)
6. Document new architecture

**Success criteria**:
- All example prompts feel better
- No regressions in execution tasks
- System feels LLM-led

---

### Tight Scope

**What we're changing**:
- Front-door ownership (LLM first)
- Context assembly (LLM-driven)
- Fast action path (new)
- Conversational responses (LLM-generated)

**What we're NOT changing**:
- Supervisor loop architecture
- Planner/router/agent structure
- Tool registry and invocations
- Memory system
- Review and evaluation
- State management

**This is surgical refactoring, not a rewrite.**

---

## 10. RISK CHECK

### What Could Break If Done Badly

#### **Risk 1: LLM Unavailable Path**
**Scenario**: LLM provider goes down, all requests fail

**Mitigation**:
- Keep deterministic fallbacks intact
- Test with `LLM_ENABLED=false` environment variable
- Fallback should feel basic but functional

**Test plan**:
- Disable OpenRouter in config
- Verify all prompts get deterministic responses
- Verify no crashes or infinite loops

---

#### **Risk 2: Context Assembly Breaks**
**Scenario**: LLM requests context that doesn't exist or fails to request needed context

**Mitigation**:
- Define clear context types: "task_state", "memory", "capabilities", "tools"
- Default to including minimal context if LLM request parsing fails
- Log context mismatches for tuning

**Test plan**:
- Mock LLM responses with various context_needs
- Verify context assembly handles missing/invalid requests
- Verify responses are coherent with thin context

---

#### **Risk 3: Fast Action Path Misroutes**
**Scenario**: Complex task gets routed to fast action path, fails

**Mitigation**:
- Conservative fast action detection
- If fast action execution fails, escalate to supervisor
- Explicit list of supported fast actions (reminders, status, etc.)

**Test plan**:
- Try edge cases: "remind me when you're done building the app" (should NOT be fast)
- Try complex reminders: "remind me every day at 3pm" (should escalate)
- Verify escalation path works

---

#### **Risk 4: Response Quality Degrades**
**Scenario**: LLM-generated responses are worse than Python templates

**Mitigation**:
- Tune LLM prompts with examples
- A/B test LLM vs deterministic responses
- Keep deterministic fallback as safety net

**Test plan**:
- Compare response quality for 50+ test prompts
- User feedback: does it feel better?
- Measure: response coherence, relevance, tone

---

#### **Risk 5: Latency Increases**
**Scenario**: LLM-first adds latency, system feels slower

**Mitigation**:
- Thin context for first-pass reduces LLM latency
- Fast action path skips supervisor, reduces latency
- Measure: target < 2s for fast actions, < 5s for tasks

**Test plan**:
- Benchmark: time from user message to first response
- Target: fast actions < 2s, conversational < 3s, tasks < 5s
- Profile: where is time spent?

---

### How to Preserve Current Functionality

**Guarantee 1: Execution Tasks Still Work**
- Supervisor loop is unchanged
- Planner/router/agent flow is unchanged
- Review and evaluation are unchanged

**Guarantee 2: Deterministic Fallback Exists**
- When LLM unavailable, system falls back to Python logic
- Fallback should feel basic but functional

**Guarantee 3: Tool Invocations Are Safe**
- Tool registry validation is unchanged
- Execution boundaries are unchanged
- Evidence requirements are unchanged

**Guarantee 4: Memory Capture Works**
- Memory capture in `operator_context` is unchanged
- Open loop tracking is unchanged

---

### How to Preserve Honesty Guarantees

**The refactor does NOT change**:
- Review agent validation
- Evidence requirements
- Completion confidence scoring
- Blocker surfacing

**The refactor DOES change**:
- Who decides what context is relevant (LLM, not Python)
- Who owns the response (LLM, not Python)
- How fast actions are handled (direct, not orchestrated)

**Honesty is preserved because**:
- Execution tasks still go through full supervisor loop
- Review agent still validates outputs
- Evaluator still judges completion based on evidence
- Fast actions are explicitly simple (reminder, status, etc.)

---

## FINAL SUMMARY

### User Diagnosis: CORRECT

The system is "Python decides, LLM narrates" not "LLM decides, Python executes."

### Problem: ARCHITECTURAL BUT FIXABLE

The front-door ownership lives in the wrong layer. Python pattern matching runs before the LLM. Context is Python-assembled. Responses are Python-templated.

### Fix: TARGETED REFACTOR, NOT REWRITE

**4 core changes**:
1. LLM first-pass interpretation (remove quick-answer bypass)
2. LLM-driven context assembly (thin context, LLM requests more)
3. Fast action path (bypass supervisor for simple actions)
4. Remove Python overcontrol (let LLM own responses)

**Effort**: 4 weeks, 9 files, ~1500 lines changed

**Impact**: System feels LLM-led, assistant-like, intelligent

### This Fixes

- ✅ Greetings feel natural, not contaminated
- ✅ Memory questions don't leak task state
- ✅ Simple actions feel instant and lightweight
- ✅ LLM owns interpretation and response
- ✅ Python executes underneath, doesn't shape interaction

### This Preserves

- ✅ Supervisor/planner/router architecture
- ✅ Review and evaluation
- ✅ Evidence requirements
- ✅ Honesty guarantees
- ✅ Deterministic fallback when LLM unavailable

---

**END OF AUDIT REPORT**
