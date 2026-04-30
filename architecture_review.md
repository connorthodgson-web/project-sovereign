# PROJECT SOVEREIGN: ARCHITECTURE REVIEW
## Strategy + Architecture Truth Pass

**Date**: 2026-04-22  
**Reviewer**: Architecture Analysis Agent  
**Primary Sources**: AGENTS.md, codebase analysis, Apex-style operator model

---

## EXECUTIVE SUMMARY

**Current State**: Project Sovereign is an **honest orchestration prototype with strong assistant-first behavior** but is **overbuilding custom infrastructure** where prebuilt tools should be used.

**Biggest Architectural Mistake**: Building a custom memory store, retrieval system, context assembly layer, and operator context service when **Zep** should provide all of this. You are rebuilding memory infrastructure instead of connecting to it.

**Highest-Leverage Fix**: **Stop building memory infrastructure. Integrate Zep immediately.** This unlocks real memory without maintaining custom retrieval, ranking, storage, and context assembly logic.

**Next Implementation Pass**: Zep integration + LangGraph foundation (not full CEO expansion yet).

---

## ALIGNMENT ANALYSIS

### ✅ **What Aligns with AGENTS.md**

1. **LLM-First Planning**: Planner uses LLM with deterministic fallback
2. **Honest Status Reporting**: Agents return `PLANNED`, `SIMULATED`, `BLOCKED`, `COMPLETED` truthfully
3. **One Operator Feel**: Assistant layer provides unified interface
4. **Goal → Plan → Execute → Review**: Supervisor implements this loop
5. **Strong Memory Philosophy**: System captures conversation, facts, open loops
6. **Low Python Decision Logic** (partially): Planning and routing can use LLM
7. **Real Tool Execution**: `file_tool` and `runtime_tool` actually work

### ❌ **What Violates AGENTS.md**

1. **"Glue Over Reinvention" (VIOLATED)**: You are rebuilding memory infrastructure, retrieval, context assembly, and operator context management instead of using existing tools
2. **"Dynamic Subagent Creation" (MISSING)**: Agents are static, no temporary task-specific agents
3. **"LLM-Driven Orchestration" (PARTIAL)**: Router still uses Python keyword scoring as primary path, LLM is fallback
4. **"Tool Philosophy" (VIOLATED)**: Not using LangGraph for orchestration, not using Zep for memory
5. **"Modular and Swappable" (WEAK)**: Memory backend is tightly coupled, no abstraction for swapping backends

---

## APEX-STYLE OPERATOR MODEL ALIGNMENT

### ✅ **What Aligns**

- One supervisor/operator at the top
- Honest about scaffolded vs live capabilities
- Tools underneath (file, runtime, reminders)
- Clean API-driven interface (FastAPI + Slack)

### ❌ **What Misses**

- **Fake delegation**: Most agents are simulation shells, not real capabilities
- **Overbuilt routing**: Router is too complex for current capability level
- **Minimal fake delegation** (VIOLATED): Research agent, reviewer agent, communications agent are simulated
- **Focus on connecting tools, not rebuilding them** (VIOLATED): Memory, retrieval, context assembly are custom-built

**Verdict**: The system *looks* like an Apex-style operator but **most agents are fake**. It should either have fewer agents with real capabilities, or more transparent "coming soon" messaging.

---

## ASSISTANT-FIRST PRIORITY ANALYSIS

### ✅ **Assistant Layer is Strong**

The assistant layer (`core/assistant.py`, `core/conversation.py`) is **excellent**:
- Natural conversational handling
- LLM-assisted + deterministic decision-making
- Request mode classification (ANSWER / ACT / EXECUTE)
- Escalation levels (conversational_advice → single_action → bounded_task_execution → objective_completion)
- Context-aware responses
- Preference tracking

This is the **strongest part of the codebase**.

### ❌ **Overbuilding Infrastructure Before Assistant is Strong Enough**

**Problems**:
1. **Custom memory store** (625 lines) with custom retrieval, ranking, bigram matching, recency scoring
2. **Operator context service** managing runtime snapshots, facts, open loops
3. **Context assembly layer** with prompt library and instruction files
4. **Capability manifest** tracking readiness across integrations
5. **Agent catalog** with metadata
6. **Tool registry** (minimal, acceptable)
7. **Planner** with tool invocation builders
8. **Router** with keyword scoring

**These are all infrastructure concerns that should be handled by:**
- **Zep** (memory, retrieval, context)
- **LangGraph** (orchestration, state management, agent coordination)
- **OpenRouter** (LLM reasoning)

**Verdict**: You are building infrastructure instead of improving assistant feel.

---

## STEP 1: PRODUCT-CRITICAL CUSTOM LOGIC

### ✅ **What MUST Remain Custom**

These define Sovereign's identity and should stay:

#### **A. Assistant Behavior**
- `core/assistant.py` - Request interpretation (ANSWER/ACT/EXECUTE), escalation logic
- `core/conversation.py` - Conversational response composition
- `core/system_context.py` - Identity and capabilities messaging

#### **B. Orchestration Flow**
- `core/supervisor.py` - Main operator loop (but simplify with LangGraph)
- `core/evaluator.py` - Goal satisfaction evaluation
- `core/fast_actions.py` - Quick action routing

#### **C. Memory Policy**
- **What to remember** (not how to store it)
- **When to capture facts** (user preferences, project context, operational memory)
- **Categories and confidence** (current weights in `memory_store.py`)
- **Continuity logic** (open loops, active tasks)

#### **D. User-Facing Behavior**
- Slack interface personality
- Response tone and formatting
- Reminder delivery UX
- Progress indication logic

#### **E. Integration Adapters**
- `integrations/reminders/adapter.py` - Reminder scheduling contract
- `integrations/slack_outbound.py` - Slack message delivery
- `agents/reminder_agent.py` - Reminder request parsing

**Total Custom LOC to Keep**: ~2,000 lines (assistant + orchestration policy)

---

## STEP 2: REINVENTED INFRASTRUCTURE (TO REPLACE)

### ❌ **Memory Storage & Retrieval** → Replace with **Zep**

**Current State** (825+ lines):
- `memory/memory_store.py` (625 lines) - JSON file-backed storage
- `memory/retrieval.py` (119 lines) - Keyword retrieval with scoring
- `core/operator_context.py` (partial) - Context assembly

**Problems**:
1. Custom keyword-based retrieval (tokenization, bigrams, relevance scoring)
2. Manual recency scoring
3. Custom fact ranking with category weights
4. No semantic search
5. File-based persistence (doesn't scale)
6. No multi-user support
7. Reinventing memory infrastructure

**Zep Provides**:
- Vector-based semantic search
- Automatic summarization
- Fact extraction
- Session management
- Multi-user memory isolation
- Persistent storage
- Retrieval with ranking
- Context assembly

**Should Zep Replace?**: **YES, 100%**

**Migration Path**:
1. Replace `MemoryStore` with Zep client
2. Keep `MemoryFact` structure as contract but store in Zep
3. Use Zep's session API for conversation turns
4. Use Zep's fact extraction for user/project/operational facts
5. Use Zep's search for retrieval
6. Keep category weights and confidence as metadata in Zep

**What Breaks**:
- Direct JSON file reads (good riddance)
- Custom keyword retrieval (Zep's is better)
- Some unit tests (rewrite to use Zep client)

---

### ❌ **Orchestration Scaffolding** → Replace with **LangGraph**

**Current State** (600+ lines):
- `core/supervisor.py` - Custom loop with iteration budget, dependency resolution
- `core/planner.py` - Subtask decomposition with custom dependency linking
- `core/router.py` - Agent assignment with scoring
- `core/state.py` - In-memory task state store
- `core/models.py` - Custom state models

**Problems**:
1. Custom iteration loop with manual `while iterations < max_iterations`
2. Manual dependency tracking (`depends_on` field)
3. No parallel execution
4. No state persistence beyond in-memory
5. No visualization of execution graph
6. Manual context passing between agents
7. Custom routing with keyword scoring (should be LLM-first)

**LangGraph Provides**:
- State management (StateGraph)
- Node-based orchestration
- Conditional edges (routing)
- Built-in persistence (checkpoints)
- Streaming support
- Visualization (mermaid diagrams)
- Parallel execution
- Tool calling support

**Should LangGraph Replace?**: **YES** (but incrementally)

**Migration Path**:
1. Keep current Supervisor for now
2. Add LangGraph as a layer *underneath* Supervisor
3. Migrate Planner → LangGraph planning node
4. Migrate Router → LangGraph conditional edges
5. Migrate agent execution → LangGraph tool nodes
6. Eventually: Supervisor becomes a thin StateGraph coordinator

**What Breaks**:
- Current supervisor loop (rewrite as graph)
- In-memory state store (migrate to LangGraph checkpoints)
- Custom dependency resolution (use LangGraph edges)

---

### ❌ **Context Assembly** → Simplify with **Zep**

**Current State** (236 lines):
- `core/context_assembly.py` - Builds prompt bundles with instructions, runtime state, capabilities
- `core/operator_context.py` (partial) - Runtime snapshot builder
- `core/prompt_library.py` - Loads instruction files

**Problems**:
1. Custom runtime snapshot logic
2. Manual context profile inference
3. Prompt template assembly
4. Should be using Zep's context assembly

**Zep Provides**:
- Automatic context assembly
- Relevant memory injection
- Session summarization

**Should Keep**:
- Instruction file loading (identity, capabilities)
- Role-specific prompt templates
- System context (`SOVEREIGN_SYSTEM_CONTEXT`)

**Should Replace**:
- Runtime snapshot building → Zep session context
- Memory fact injection → Zep retrieval
- Context profile inference → Zep's context management

---

### ❌ **Tool Registry** → Keep (It's Minimal)

**Current State** (55 lines):
- `tools/registry.py` - Simple tool lookup and execution

**Verdict**: This is appropriately minimal. Keep it.

---

### ❌ **Agent Catalog** → Keep (It's Metadata)

**Current State**:
- `agents/catalog.py` - Agent definitions with capabilities

**Verdict**: This is metadata. Keep it for now.

---

## STEP 3: ZEP MEMORY INTEGRATION ANALYSIS

### **What Should Stay Custom** (Policy, Not Storage)

1. **Memory Capture Logic**:
   - When to remember (after task completion, user preferences, open loops)
   - Fact categories (preference, priority, decision, identity, etc.)
   - Confidence scoring rules
   - Layer separation (user, project, operational)

2. **Memory Policy**:
   - Category weights (preference: 1.55, priority: 1.45, etc.)
   - Recency scoring strategy
   - Transient memory pruning rules
   - What constitutes "stale" memory

3. **Open Loop Management**:
   - When to create open loops
   - When to close them
   - Open loop summarization

### **What Should Be Replaced** (Storage, Retrieval, Context)

1. **Storage Backend**: Zep replaces JSON file
2. **Retrieval**: Zep's semantic search replaces keyword matching
3. **Context Assembly**: Zep assembles relevant context
4. **Fact Ranking**: Zep ranks facts (can use metadata for custom weights)
5. **Session Management**: Zep manages conversation turns

### **How Zep Should Be Integrated**

**Architecture**:
```
OperatorContextService (custom)
  ├─> captures facts, open loops, preferences (custom policy)
  ├─> stores via ZepClient (replaces MemoryStore)
  └─> retrieves via ZepClient.search() (replaces KeywordRetrieval)

Zep Backend
  ├─> sessions (conversation turns)
  ├─> facts (user, project, operational)
  ├─> memory (semantic search)
  └─> summaries (automatic)
```

**Migration Steps**:
1. Install `zep-python` SDK
2. Create `ZepMemoryAdapter` implementing same interface as `MemoryStore`
3. Map `MemoryFact` → Zep fact with metadata (layer, category, confidence)
4. Map conversation turns → Zep session messages
5. Replace `memory_store` singleton with `zep_memory_adapter`
6. Replace `KeywordRetrievalBackend` with `ZepRetrievalBackend`
7. Keep `OperatorContextService` for policy, route storage to Zep

**What Would Break**:
- Direct `memory_store.snapshot()` calls → Use Zep client
- Custom keyword search → Use Zep semantic search (better)
- JSON file operations → Use Zep API (better)
- Unit tests → Rewrite with Zep client mocks

**Safest Migration Path**:
1. **Phase 1**: Add Zep client alongside existing memory store (dual-write)
2. **Phase 2**: Read from Zep, fall back to JSON if Zep fails
3. **Phase 3**: Primary read/write to Zep, remove JSON fallback
4. **Phase 4**: Delete `memory/memory_store.py` and `memory/retrieval.py`

**USER ACTION REQUIRED**:
- Sign up for Zep Cloud OR self-host Zep server
- Get Zep API key
- Set `ZEP_API_KEY` and `ZEP_API_URL` environment variables

---

## STEP 4: LANGGRAPH / ORCHESTRATION DECISION

### **Should LangGraph Be Added?**

**Answer**: **YES, but incrementally as a foundation layer, NOT for full multi-agent orchestration yet.**

### **Current Orchestration State**

**Good**:
- Clean supervisor loop
- Dependency resolution works
- Honest about execution status
- Bounded iteration limit

**Bad**:
- Custom loop logic (fragile)
- No persistence (in-memory only)
- No parallel execution
- No visualization
- Router is keyword-based (should be LLM-first)

### **What LangGraph Should Replace** (Eventually)

1. **Supervisor Loop** → LangGraph StateGraph
2. **Planner** → Planning node
3. **Router** → Conditional edges
4. **Agent Execution** → Tool nodes
5. **State Management** → LangGraph checkpoints

### **What LangGraph Should NOT Do Yet**

❌ Don't build full multi-agent CEO architecture yet  
❌ Don't expand to dynamic subagent creation yet  
❌ Don't add complex multi-turn refinement loops yet  
❌ Don't overengineer orchestration before assistant feels real

### **Minimum Orchestration Structure Needed Right Now**

**For Assistant Phase**:
1. **Simple StateGraph**:
   - `decide_mode` node (ANSWER / ACT / EXECUTE)
   - `answer` node (conversational response)
   - `act` node (single action)
   - `execute` node (bounded task)
   - Conditional edges based on mode

2. **State Schema**:
   - User message
   - Decision (mode + escalation)
   - Results
   - Response

3. **Persistence**:
   - LangGraph checkpoint (SQLite for now)
   - Can resume interrupted tasks

**Verdict**: Add LangGraph as a **foundation layer** but keep current Supervisor orchestration for now. Migrate incrementally.

### **Migration Plan**

**Phase 1**: Foundation (NOW)
- Install LangGraph
- Create simple StateGraph with decision node
- Run assistant layer through LangGraph
- Keep current Supervisor for task execution

**Phase 2**: Planning (LATER)
- Migrate Planner to LangGraph planning node
- Use LangGraph edges for routing
- Keep agent execution in Supervisor

**Phase 3**: Execution (MUCH LATER)
- Migrate agent execution to LangGraph tool nodes
- Add parallel execution
- Add persistence via checkpoints
- Remove custom Supervisor loop

**IMPORTANT**: Don't do Phase 2-3 until **assistant feel is strong** and **Zep is integrated**.

---

## STEP 5: TOOL VS CUSTOM DECISION MATRIX

| Area | Keep Custom | Replace with Tool | Hybrid | Justification |
|------|-------------|-------------------|--------|---------------|
| **Memory** | Policy, categories, weights | ✅ Zep (storage, retrieval) | Hybrid | Policy is product logic; storage/retrieval is infrastructure |
| **Routing** | Escalation logic, mode classification | ✅ LLM-first (via LangGraph) | Hybrid | Routing should be LLM-driven per AGENTS.md; keep guardrails |
| **Tool Registry** | ✅ Keep | - | - | Already minimal (55 lines), appropriate abstraction |
| **Browser** | Agent adapter | ✅ Browser-Use | Hybrid | Browser-Use does the work; agent wraps it |
| **Coding** | Agent logic, file/runtime tools | Keep (already working) | - | Tools are real and working; don't replace |
| **Scheduling** | Adapter | ✅ APScheduler (already using) | Hybrid | APScheduler does scheduling; adapter wraps it |
| **Messaging** | - | ✅ Slack SDK (already using) | - | Already using Slack SDK correctly |
| **Orchestration** | Supervisor policy | ✅ LangGraph (state, graph, persistence) | Hybrid | LangGraph handles state; Supervisor sets policy |
| **Context Assembly** | Instructions, identity | ✅ Zep (runtime context) | Hybrid | Zep assembles context; custom adds identity/instructions |
| **Conversation** | ✅ Keep | - | - | This is product behavior, not infrastructure |
| **State Management** | - | ✅ LangGraph checkpoints | Replace | No need for custom in-memory state |
| **Agent Catalog** | ✅ Keep | - | - | Metadata only, minimal |
| **Capability Manifest** | ✅ Keep (simplify) | - | - | Readiness tracking is product logic |
| **LLM Reasoning** | - | ✅ OpenRouter (already using) | - | Already correctly using OpenRouter |

---

## STEP 6: IDENTIFY OVERBUILDING

### **Where the System is Too Complex for Current Stage**

1. **Memory Infrastructure** (OVERBUILT)
   - 625 lines of custom memory store
   - Custom retrieval with tokenization, bigrams, relevance scoring
   - Should be using Zep

2. **Context Assembly** (OVERBUILT)
   - 236 lines of prompt context assembly
   - Custom runtime snapshots
   - Should be using Zep's context management

3. **Operator Context Service** (OVERBUILT)
   - Managing facts, open loops, reminders, active tasks
   - Custom snapshot building
   - Should be using Zep + LangGraph state

4. **Capability Manifest** (ACCEPTABLE but heavy)
   - Tracks readiness for 13 integrations
   - Most integrations are scaffolded
   - Keep for now, but simplify once integrations are actually wired

5. **Planner Tool Invocation Builders** (PREMATURE)
   - `FileToolInvocationBuilder`, `RuntimeToolInvocationBuilder`
   - Trying to parse goals into tool calls
   - Should let LLM/agent decide tool usage

6. **Agent Simulations** (FAKE)
   - `research_agent`, `reviewer_agent`, `communications_agent` are simulation shells
   - Not actually doing work
   - Either wire them to real tools or remove them

### **Building Ahead of Need**

1. **Dynamic tool invocation planning** - Planner tries to extract tool calls from goals, but agents don't use them consistently
2. **Objective state tracking** - Complex `ObjectiveState` model with stages, delegated agents, evidence log - too heavy for current execution
3. **Multiple memory layers** (user, project, operational) - Good idea but overbuilt before memory backend is solid

### **Simulating Capabilities Instead of Having Real Ones**

**Simulated Agents** (should be removed or replaced):
- `research_agent`: Returns "checked the relevant constraints" (fake)
- `reviewer_agent`: Returns "reviewed the result" (fake verification)
- `communications_agent`: Drafts messages but doesn't send them

**Verdict**: Remove fake agents or make them honest placeholders that say "not yet implemented".

### **Python Control Instead of LLM Reasoning**

**Router** (`core/router.py`):
- Primary routing is keyword scoring (`_browser_score`, `_reminder_score`, etc.)
- LLM routing is fallback (`_classify_with_llm`)
- **Should be LLM-first** per AGENTS.md

**Planner** (`core/planner.py`):
- Has deterministic fallback planning (`_create_fallback_plan`)
- Good for resilience, but primary path should be LLM

**Verdict**: These are acceptable for now (resilience is good), but the **primary path** should be LLM-driven.

---

## STEP 7: IDEAL NEAR-TERM ARCHITECTURE

### **What Sovereign Should Look Like AFTER Cleanup**

**Assistant-Ready Foundation for Future Apex System**

```
┌─────────────────────────────────────────────────────┐
│ USER INTERFACE (Slack, Web Dashboard)              │
└────────────────┬────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────┐
│ ASSISTANT LAYER (Custom)                            │
│  - Request interpretation (ANSWER/ACT/EXECUTE)      │
│  - Conversational response composition              │
│  - Escalation logic                                 │
│  - User preferences                                 │
└────────────────┬────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────┐
│ LANGGRAPH ORCHESTRATION (Tool)                      │
│  - State management (StateGraph)                    │
│  - Node-based execution (decide → plan → execute)   │
│  - Conditional routing (LLM-driven)                 │
│  - Persistence (checkpoints)                        │
└────────────────┬────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────┐
│ MEMORY (Zep)                                        │
│  - Semantic search                                  │
│  - Fact extraction                                  │
│  - Session management                               │
│  - Context assembly                                 │
└─────────────────────────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────────┐
│ TOOL LAYER (Mix of Custom + External)              │
│  - file_tool (custom, working) ✅                   │
│  - runtime_tool (custom, working) ✅                │
│  - reminder_tool (custom + APScheduler) ✅          │
│  - browser_tool (Browser-Use) 🔧                    │
│  - email_tool (SendGrid/Resend) 🔧                  │
│  - calendar_tool (Google Calendar API) 🔧           │
└─────────────────────────────────────────────────────┘
```

### **What Gets Removed**

❌ `memory/memory_store.py` (625 lines)  
❌ `memory/retrieval.py` (119 lines)  
❌ `core/operator_context.py` (partial, ~200 lines)  
❌ `core/context_assembly.py` (partial, ~100 lines)  
❌ Custom in-memory state management  
❌ Fake agent simulations (research_agent shell, reviewer_agent shell)

**Total Deleted**: ~1,000+ lines

### **What Gets Simplified**

🔧 `core/supervisor.py` - Becomes StateGraph coordinator  
🔧 `core/planner.py` - Becomes planning node  
🔧 `core/router.py` - Becomes conditional edge logic  
🔧 `core/models.py` - Remove some state models (LangGraph handles)

### **What Stays**

✅ `core/assistant.py` - Request interpretation  
✅ `core/conversation.py` - Response composition  
✅ `core/fast_actions.py` - Quick action routing  
✅ `core/system_context.py` - Identity  
✅ `tools/file_tool.py` - Working tool  
✅ `tools/runtime_tool.py` - Working tool  
✅ `agents/reminder_agent.py` - Reminder parsing + APScheduler adapter  
✅ `integrations/slack_client.py` - Slack interface  
✅ `integrations/slack_outbound.py` - Slack delivery  

---

## STEP 8: MIGRATION PLAN

### **Priority Order**

1. **Stop building custom memory infrastructure** (IMMEDIATE)
2. **Integrate Zep for memory** (WEEK 1)
3. **Add LangGraph foundation** (WEEK 2)
4. **Simplify/remove fake agents** (WEEK 2)
5. **Wire Browser-Use** (WEEK 3)
6. **Improve assistant feel with better memory** (ONGOING)

---

### **PHASE 1: Zep Integration** (HIGHEST PRIORITY)

#### **Step 1.1: Setup Zep**

**USER ACTION REQUIRED**:
```bash
# Option A: Use Zep Cloud (recommended)
# 1. Sign up at https://www.getzep.com
# 2. Get API key
# 3. Set environment variables:
export ZEP_API_KEY="your-zep-api-key"
export ZEP_API_URL="https://api.getzep.com"

# Option B: Self-host Zep (advanced)
# 1. Run Zep server via Docker:
docker run -p 8000:8000 ghcr.io/getzep/zep:latest
# 2. Set environment variable:
export ZEP_API_URL="http://localhost:8000"
```

Install Zep SDK:
```bash
pip install zep-python
```

Add to `requirements.txt`:
```
zep-python>=2.0.0
```

#### **Step 1.2: Create Zep Adapter**

Create `memory/zep_adapter.py`:
```python
from zep_python import ZepClient, Memory, Message
from memory.memory_store import MemoryFact, ConversationTurn

class ZepMemoryAdapter:
    def __init__(self, api_key: str, api_url: str):
        self.client = ZepClient(api_key=api_key, base_url=api_url)
        self.session_id = "sovereign-session"  # Per-user later
    
    def record_turn(self, role: str, content: str):
        self.client.memory.add_message(
            session_id=self.session_id,
            message=Message(role=role, content=content)
        )
    
    def upsert_fact(self, layer: str, category: str, key: str, value: str, confidence: float, source: str):
        self.client.memory.add_fact(
            session_id=self.session_id,
            fact=value,
            metadata={
                "layer": layer,
                "category": category,
                "key": key,
                "confidence": confidence,
                "source": source
            }
        )
    
    def search_facts(self, query: str, layers: tuple[str, ...] | None = None) -> list[MemoryFact]:
        search_results = self.client.memory.search(
            session_id=self.session_id,
            text=query,
            limit=8
        )
        # Convert Zep facts to MemoryFact format
        return [self._convert_to_memory_fact(fact) for fact in search_results]
```

#### **Step 1.3: Dual-Write Migration**

Update `memory/memory_store.py`:
```python
class MemoryStore:
    def __init__(self):
        self._json_store = MemorySnapshot()  # Keep for now
        self._zep_adapter = ZepMemoryAdapter(...)  # Add Zep
    
    def upsert_fact(self, ...):
        # Write to both
        self._json_store.upsert_fact(...)
        self._zep_adapter.upsert_fact(...)
    
    def search_facts(self, query: str):
        # Read from Zep, fall back to JSON
        try:
            return self._zep_adapter.search_facts(query)
        except Exception:
            return self._json_store.search_facts(query)
```

#### **Step 1.4: Test Zep Integration**

```bash
# Test memory capture and retrieval
python -m pytest tests/test_zep_integration.py -v
```

#### **Step 1.5: Remove JSON Store**

Once Zep is working:
1. Remove JSON file writes
2. Delete `memory/memory_store.py` (old implementation)
3. Rename `memory/zep_adapter.py` → `memory/memory_store.py`
4. Update imports

---

### **PHASE 2: LangGraph Foundation** (WEEK 2)

#### **Step 2.1: Install LangGraph**

```bash
pip install langgraph langchain-core
```

Add to `requirements.txt`:
```
langgraph>=0.2.0
langchain-core>=0.3.0
```

#### **Step 2.2: Create Simple StateGraph**

Create `core/state_graph.py`:
```python
from langgraph.graph import StateGraph
from typing import TypedDict

class SovereignState(TypedDict):
    user_message: str
    mode: str  # ANSWER / ACT / EXECUTE
    escalation: str
    response: str
    results: list

def decide_mode(state: SovereignState):
    # Use assistant layer to decide
    decision = assistant_layer.decide(state["user_message"])
    return {"mode": decision.mode, "escalation": decision.escalation_level}

def answer(state: SovereignState):
    # Conversational response
    response = conversational_handler.handle(state["user_message"])
    return {"response": response}

def act(state: SovereignState):
    # Single action
    result = fast_action_handler.handle(state["user_message"])
    return {"response": result}

def execute(state: SovereignState):
    # Bounded task execution
    result = supervisor.handle_user_goal(state["user_message"])
    return {"response": result}

# Build graph
workflow = StateGraph(SovereignState)
workflow.add_node("decide", decide_mode)
workflow.add_node("answer", answer)
workflow.add_node("act", act)
workflow.add_node("execute", execute)

workflow.set_entry_point("decide")
workflow.add_conditional_edges(
    "decide",
    lambda state: state["mode"],
    {
        "answer": "answer",
        "act": "act",
        "execute": "execute"
    }
)

app = workflow.compile()
```

#### **Step 2.3: Use StateGraph in API**

Update `api/routes/chat.py`:
```python
from core.state_graph import app as workflow

@router.post("/chat")
def chat(request: ChatRequest):
    result = workflow.invoke({"user_message": request.message})
    return result["response"]
```

#### **Step 2.4: Add Persistence**

```python
from langgraph.checkpoint.sqlite import SqliteSaver

memory = SqliteSaver.from_conn_string("sovereign.db")
app = workflow.compile(checkpointer=memory)
```

---

### **PHASE 3: Simplify/Remove Fake Agents** (WEEK 2)

#### **Step 3.1: Remove Simulated Agents**

Replace simulation shells with honest placeholders:

**Before** (`agents/research_agent.py`):
```python
return AgentResult(
    status=AgentExecutionStatus.SIMULATED,
    summary="Checked the relevant constraints",
    ...
)
```

**After**:
```python
return AgentResult(
    status=AgentExecutionStatus.PLANNED,
    summary="Research capability is not yet implemented.",
    blockers=["Research agent needs: web search API, fact extraction, synthesis"],
    next_actions=["Wire a search API (Tavily, Serper, etc.)", "Add synthesis logic"]
)
```

#### **Step 3.2: Remove Non-Essential Agents**

For now, keep only:
- ✅ `memory_agent` (working)
- ✅ `coding_agent` (working with file_tool, runtime_tool)
- ✅ `reminder_agent` (working with APScheduler)
- 🔧 `browser_agent` (scaffolded, wire Browser-Use)
- ❌ `research_agent` (remove or make honest)
- ❌ `reviewer_agent` (remove or make honest)
- ❌ `communications_agent` (remove or make honest)

---

### **PHASE 4: Wire Browser-Use** (WEEK 3)

#### **Step 4.1: Install Browser-Use**

**USER ACTION REQUIRED**:
```bash
# Get Browser-Use API key from browserbase.com or similar
export BROWSER_USE_API_KEY="your-api-key"
```

```bash
pip install browser-use
```

#### **Step 4.2: Update Browser Agent**

Replace scaffolded implementation:

```python
from browser_use import BrowserUse

class BrowserAgent(BaseAgent):
    def __init__(self):
        self.browser = BrowserUse(api_key=settings.browser_use_api_key)
    
    def run(self, task: Task, subtask: SubTask) -> AgentResult:
        try:
            result = self.browser.execute(subtask.objective)
            return AgentResult(
                status=AgentExecutionStatus.COMPLETED,
                summary=f"Executed browser task: {subtask.objective}",
                evidence=[result.screenshot, result.text],
                ...
            )
        except Exception as e:
            return AgentResult(
                status=AgentExecutionStatus.BLOCKED,
                summary=f"Browser execution failed: {e}",
                blockers=[str(e)]
            )
```

---

### **PHASE 5: What to Delay**

❌ **Don't Do Yet**:
1. Full CEO/multi-agent orchestration
2. Dynamic subagent creation
3. Complex multi-turn refinement loops
4. Email/calendar integrations (wait until assistant is strong)
5. Web dashboard (Slack is enough for now)
6. Voice/call support
7. Semantic retrieval (Zep handles this)
8. Custom model routing

---

## STEP 9: FINAL VERDICT

### **1. Are We Overbuilding?**

**YES.** You are building custom memory infrastructure, retrieval, context assembly, and state management instead of using Zep and LangGraph.

**Overbuilt Areas**:
- Memory store (625 lines) → Zep
- Retrieval system (119 lines) → Zep
- Context assembly (236 lines) → Zep + minimal custom
- Operator context service (partial) → Zep + LangGraph
- Custom state management → LangGraph

**Total Overbuilt LOC**: ~1,000+ lines

### **2. Are We Using Enough Existing Tools?**

**NO.** You are not using:
- ❌ **Zep** for memory (should be primary dependency)
- ❌ **LangGraph** for orchestration (should be foundation)
- ❌ **Browser-Use** for browser automation (scaffolded but not wired)

You ARE correctly using:
- ✅ **OpenRouter** for LLM reasoning
- ✅ **APScheduler** for reminder scheduling
- ✅ **Slack SDK** for messaging
- ✅ **FastAPI** for API

### **3. What is the Biggest Architectural Mistake Right Now?**

**Building a custom memory store and retrieval system instead of using Zep.**

This violates AGENTS.md principle: "Glue Over Reinvention."

You are spending engineering effort maintaining:
- Custom storage (JSON files)
- Custom retrieval (keyword matching, tokenization, bigrams)
- Custom ranking (category weights, recency scoring)
- Custom context assembly

**All of this is infrastructure that Zep provides.**

### **4. What is the Highest-Leverage Fix?**

**Integrate Zep immediately.**

This unlocks:
- ✅ Semantic search (better than keyword)
- ✅ Automatic summarization
- ✅ Fact extraction
- ✅ Multi-user memory isolation
- ✅ Persistent storage
- ✅ Context assembly
- ✅ No maintenance of custom retrieval logic

**Impact**: Delete ~1,000 lines of custom code, gain better memory capabilities.

### **5. What Should the NEXT Implementation Pass Be?**

**Zep Integration + LangGraph Foundation**

**NOT** full CEO/multi-agent expansion.

**Focus**:
1. Replace memory store with Zep (WEEK 1)
2. Add LangGraph StateGraph foundation (WEEK 2)
3. Simplify/remove fake agents (WEEK 2)
4. Wire Browser-Use (WEEK 3)
5. Improve assistant feel with better memory (ONGOING)

**Success Criteria**:
- ✅ Memory retrieval uses Zep semantic search
- ✅ Conversation turns persist in Zep
- ✅ Facts stored in Zep with metadata
- ✅ LangGraph handles state management
- ✅ Assistant responses use Zep context
- ✅ Browser agent executes real tasks via Browser-Use
- ✅ No fake simulated agents

---

## CONCLUSION

Project Sovereign has a **strong assistant layer** and **correct philosophy** (AGENTS.md-aligned), but is **overbuilding infrastructure** instead of connecting to existing tools.

**The system should be:**
- **Custom**: Assistant behavior, memory policy, orchestration policy
- **Tool-backed**: Memory (Zep), orchestration (LangGraph), browser (Browser-Use), scheduling (APScheduler)

**Current state**: 70% custom, 30% tool-backed  
**Target state**: 40% custom, 60% tool-backed

**Stop building memory infrastructure. Start connecting to Zep.**

---

## APPENDIX: DETAILED FILE-BY-FILE DECISIONS

### **DELETE** (Replace with Zep)
- `memory/memory_store.py` (625 lines) → Zep storage
- `memory/retrieval.py` (119 lines) → Zep search

### **SIMPLIFY** (Keep policy, use LangGraph for state)
- `core/supervisor.py` → StateGraph coordinator
- `core/planner.py` → Planning node
- `core/router.py` → Conditional edges
- `core/operator_context.py` → Use Zep + LangGraph state
- `core/context_assembly.py` → Use Zep context + minimal custom

### **KEEP** (Product logic)
- ✅ `core/assistant.py`
- ✅ `core/conversation.py`
- ✅ `core/fast_actions.py`
- ✅ `core/evaluator.py`
- ✅ `core/system_context.py`
- ✅ `tools/file_tool.py`
- ✅ `tools/runtime_tool.py`
- ✅ `agents/reminder_agent.py`
- ✅ `integrations/slack_client.py`
- ✅ `integrations/slack_outbound.py`
- ✅ `integrations/reminders/service.py`

### **REMOVE or REPLACE** (Fake/Scaffolded)
- ❌ `agents/research_agent.py` → Make honest or remove
- ❌ `agents/reviewer_agent.py` → Make honest or remove
- ❌ `agents/communications_agent.py` → Wire to Slack outbound or remove
- 🔧 `agents/browser_agent.py` → Wire to Browser-Use

---

**END OF ARCHITECTURE REVIEW**
