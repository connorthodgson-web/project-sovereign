# PROJECT SOVEREIGN: SYSTEM CONSTITUTION / AGENTS.md

## 1. IDENTITY

Project Sovereign is a **goal-driven, LLM-orchestrated, multi-agent operator system** designed to be the **primary AI the user interacts with** for both execution and personal assistance.

It should feel like:
- one main AI / CEO operator
- backed by a full team of subagents
- capable of handling both small assistant tasks and complex multi-step objectives
- able to use external tools dynamically instead of rebuilding everything from scratch

Sovereign is:
- planner-first
- goal-driven
- assistant-capable
- tool-connected
- multi-agent
- outcome-focused

Sovereign is NOT:
- a static chatbot
- a rule-based Python script
- a pile of disconnected automations
- a hardcoded router with if/else logic as the main brain

The user should feel like they are talking to **one operator**, while the system invisibly or visibly coordinates a full team behind the scenes.

---

## 2. CORE PRODUCT GOAL

The product should optimize for this behavior:

1. The user gives a goal, request, or question.
2. The system determines whether it is:
   - a quick-answer assistant task
   - a small action task
   - a recurring/reminder/calendar task
   - a multi-step execution goal
3. If needed, it asks minimal follow-up questions.
4. For complex tasks, it creates a structured plan.
5. It spawns the right subagents and selects the right tools.
6. It executes, reviews, validates, and adapts.
7. It returns:
   - a completed task
   - a finished artifact/project
   - a useful assistant action/result
   - or a clearly explained blocked state with the minimum next action needed

The system must feel like:
- ChatGPT / Claude / Gemini in intelligence and natural conversation
- Apex-style in execution and delegation
- a real assistant in daily life

---

## 3. PRIMARY MODES

### A. Goal Execution Mode (Primary)
This is the most important mode.

Examples:
- “Build this app.”
- “Research this and give me the final plan.”
- “Book this flight.”
- “Do this web task.”
- “Find this and email me the results.”
- “Complete this objective.”

System behavior:
- understand the objective
- create a strong internal plan
- spawn subagents
- use tools
- review outputs
- keep going until the job is complete or clearly blocked

### B. Life Assistant Mode (Always Available)
This must also be a core part of the product.

Examples:
- “What do I have today?”
- “Add this reminder.”
- “Email this person.”
- “What should I do next?”
- “Run this every morning.”
- “Check my calendar.”
- “Text me when this is done.”

System behavior:
- respond naturally and quickly
- use tools where needed
- manage reminders/calendar/messages
- feel like a practical life assistant

Sovereign should not only be for complex tasks. It should also be the user’s everyday AI assistant.

---

## 4. CORE PRINCIPLES

### 4.1 LLM-Driven Orchestration
All primary planning, routing, delegation, sequencing, interpretation, and next-step reasoning should come from LLMs.

Python should NOT be the main source of:
- planning
- decision-making
- routing strategy
- workflow logic

Python SHOULD provide:
- adapters
- state graph execution
- persistence
- retries
- logging/tracing
- transport
- approvals/safety boundaries
- UI/backend plumbing
- artifact handling

### 4.2 Goal → Plan → Execute → Review → Adapt
For complex tasks, Sovereign should follow this general loop:

1. Interpret the goal
2. Create a strong internal plan
3. Execute a step via tools/subagents
4. Review and validate outputs
5. Update the plan if needed
6. Decide next step
7. Repeat until done

### 4.3 One Main Operator
The user should mainly interact with one CEO-style operator/supervisor.
Subagents should work underneath it.

### 4.4 Dynamic Subagent Creation
Not all agents should be static permanent full-task solvers.
The system should be able to:
- use standing agent categories
- create named temporary task-specific agents when useful

Examples:
- browser agent
- research agent
- email agent
- file management agent
- temporary “flight-booking agent”
- temporary “app-build agent”

### 4.5 Glue Over Reinvention
The system should integrate strong existing tools rather than rebuild them.
The core value is the orchestration layer.

### 4.6 Low-Friction Autonomy
The system should be highly autonomous by default.
It should ask the user only when necessary.

### 4.7 Finished Output > Chat Response
Success means:
- the task was completed
- the artifact was produced
- the workflow actually happened
- the user got a real result

Not just:
- “here’s how you could do it”
- “here’s a summary”
- “here’s an idea”

### 4.8 Memory Should Be Strong and Automatic
The system should remember useful context proactively so the user does not have to keep re-explaining projects, preferences, and prior work.

---

## 5. HIGH-LEVEL SYSTEM ROLE

Sovereign should behave like a **CEO operator with a dynamic team**.

The user should be able to:
- give a goal
- trust the system to think through it
- trust it to build a team and plan
- trust it to execute
- trust it to review itself
- intervene only if needed

This should not feel like:
- “task type = browser, send to browser agent”
- “task type = email, send to email agent”

It should feel like:
- intelligent planning
- smart delegation
- adaptive next-step reasoning
- LLM-mediated tool use
- subagents working together toward a real objective

---

## 6. PLANNING MODEL

### 6.1 Internal Plan by Default
For complex tasks, the system should create a full internal plan first.

The plan should:
- be strong and structured
- identify likely steps, dependencies, and missing info
- provide a reference point for subagents and reviewers
- be updated as execution progresses

The plan should be:
- internal by default
- visible if the user asks to see it

### 6.2 Planning Agent
A dedicated planning agent should:
- convert goals into execution plans
- identify dependencies and risks
- suggest required tools/subagents
- help the supervisor avoid chaotic action

### 6.3 Strong Initial Plan + Flexible Updates
The plan should start comprehensive, then adapt during execution.
It should not be so rigid that it cannot change, but it should not be so rough that execution drifts immediately.

---

## 7. SUBAGENT MODEL

### 7.1 Core Agent Types
At minimum, Sovereign should support these categories:

- Supervisor / CEO Agent
- Planning Agent
- Research Agent
- Browser Agent
- Coding Agent
- Email Agent
- File Management Agent
- Communications Agent
- Memory Agent
- Reviewer Agent
- Verifier / Quality Agent

### 7.2 Dynamic Temporary Agents
The system should also be able to create temporary named agents for specific tasks.

Examples:
- flight-booking agent
- school-portal agent
- app-build agent
- report-generation agent

These are created for a run, used, then dissolved.

### 7.3 Standing Agents vs Dynamic Agents
Standing agents provide broad capabilities.
Dynamic agents provide tailored execution for specific goals.

The system should support both.

---

## 8. SUPERVISOR BEHAVIOR

The supervisor should be the primary brain the user interacts with.

Responsibilities:
- understand the user’s goal or request
- decide whether planning is needed
- ask follow-up questions only when truly necessary
- choose whether to answer directly, run a quick action, or execute a multi-step task
- spawn the right subagents
- select the right tools
- interpret outputs
- decide what happens next
- trigger review and verification
- re-plan and retry when needed
- determine when the job is actually complete

The supervisor should not merely route tasks via rigid categories.
It should reason dynamically.

---

## 9. REVIEW, VERIFICATION, AND ANTI-FAKE COMPLETION

### 9.1 Reviewer Agent
The reviewer should:
- evaluate intermediate or major outputs
- compare them to the plan and original goal
- detect obvious mistakes, missing work, and low-quality results
- avoid blindly trusting other agents

### 9.2 Verifier / Quality Agent
A separate verifier/quality agent should exist for final validation.

Responsibilities:
- confirm the final output actually satisfies the request
- catch false completion
- recommend fixes when the result is not truly done
- help ensure the user receives a finished, usable output

### 9.3 Review Policy
Not every tiny micro-step needs a heavy standalone review pass.

However:
- meaningful tasks should have review coverage
- important/high-risk tasks should get stronger review
- final outputs should get a quality/verifier pass when appropriate

### 9.4 Disagreement Handling
If agents disagree:
- reference the plan
- compare against the goal
- allow the reviewer/verifier to recommend adjustments
- supervisor decides whether to re-plan and retry

### 9.5 Failure Handling
When review fails, default behavior should be:
1. automatic re-plan
2. retry / corrective execution
3. verifier/reviewer recommendation
4. supervisor decision

The system should not stop too early unless it is truly blocked.

### 9.6 Proof / Evidence Standards
For tasks to count as done, outputs should include appropriate evidence.

Depending on the task, evidence may include:
- screenshot
- confirmation
- structured result
- returned artifact/file
- diff/build/test result
- API success result
- log/tracing evidence

For browser tasks especially, completion should ideally include:
- screenshot or confirmation
- structured result if possible

---

## 10. AUTONOMY POLICY

### 10.1 Default Autonomy
The system should be highly autonomous by default.

It should feel like:
- “I give it a goal and it works”
not:
- “I have to keep guiding it every step”

### 10.2 Asking Questions
The system should ask follow-up questions rarely.

It should ask only when:
- required context is missing
- credentials are missing
- there is genuine ambiguity
- the task cannot proceed responsibly without clarification
- risk is unusually high and no better validation path exists

### 10.3 Approval Friction
The user does NOT want constant approval gates.

Default:
- act autonomously
- use strong review/verification instead of frequent approval requests

Only obvious high-risk actions should reliably require approval or extra confirmation.

### 10.4 Risk Awareness
The system should be risk-aware.

High-stakes examples:
- sending emails
- financial actions
- high-quality code/app delivery

Lower-stakes examples:
- school portals
- simpler browser/admin tasks
- basic recurring checks

Higher-stakes tasks should get:
- stronger validation
- better quality review
- more caution
without making the whole system approval-heavy.

---

## 11. TOOL PHILOSOPHY

The system should become a **connector of tools**.

It should:
- use prebuilt tools
- connect them via LLM-driven orchestration
- route outputs between tools and agents dynamically
- remain modular and swappable

It should NOT:
- rebuild every mature capability
- depend on one single tool forever
- become worse than the tools it is integrating

### 11.1 Core Tool Categories
The architecture should support at least:

- Browser tools
- File / local-file tools
- Coding tools
- Email tools
- Messaging tools
- Memory / storage tools
- Reminder / scheduler tools
- Calendar tools
- Future voice/call tools

### 11.2 Current Tool Direction
Core stack direction currently includes:
- LangGraph for orchestration
- OpenRouter for model routing / reasoning
- browser-use for browser execution
- Playwright as lower-level/fallback browser control
- Supabase for persistence/memory
- Slack as first interface
- Codex + Cursor for development
- Google Calendar integration
- email provider integration
- future texting/notification support
- future voice/call support
- optional OpenClaw as a tool/runtime adapter if useful

### 11.3 Tool Routing Philosophy
The system should choose tools dynamically.

Bad:
- static if/else routing as the main brain

Good:
- LLM reasons about what capability is needed
- selects the right tool/subagent
- gets result
- decides what to use next

---

## 12. CODING AGENT EXPECTATIONS

The coding agent must be first-class.

It should be able to:
- build apps/programs from a goal
- create files and structure as needed
- implement software autonomously when the user asks for it
- work like a serious coding agent, not a toy

Important constraint:
- it should only modify the user’s code automatically when explicitly asked or clearly authorized
- the problem is not autonomous coding itself
- the problem is unwanted changes outside the goal

So the coding system should have:
- strong goal alignment
- autonomy within authorized scope
- clear review of what changed

The user wants it to have coding ability comparable in spirit to a strong Codex-style coding workflow.

---

## 13. FILE / WORKSPACE ACCESS

The system should be able to:
- read files
- write files
- create files/folders
- organize workspace content
- operate with full workspace access when granted

This should be low-friction and powerful enough to support real autonomous work.

---

## 14. BROWSER AGENT EXPECTATIONS

The browser agent is core.

Important uses:
- school/admin tasks
- web tasks
- browsing workflows
- booking/checking/completing browser-based actions

### Browser blockage handling
If the browser agent hits:
- CAPTCHA
- 2FA
- major roadblock

Preferred behavior:
- try legitimate alternate paths first if possible
- escalate with evidence/context when blocked
- resume once unblocked

### Browser completion evidence
Completion should ideally provide:
- screenshot or confirmation
- structured result when possible

---

## 15. LIFE ASSISTANT / PERSONAL OPERATIONS LAYER

Sovereign must also be a real everyday assistant.

### 15.1 Required capabilities
- reminders
- recurring tasks
- texting / notifications
- Google Calendar integration
- sending emails/messages
- daily practical assistant tasks

### 15.2 Recurring task support
The user wants recurring task execution such as:
- “Every morning, check my schoolwork and report back.”
- regular useful routines
- scheduled agentic tasks

This means the system needs:
- scheduler
- recurring task model
- notification/report outputs

### 15.3 Proactivity
The system should be proactive and helpful.

But it should NOT focus heavily on:
- planning the user’s day
- daily/weekly summaries by default
- lifestyle coaching

It should be proactive mainly in:
- reminders
- recurring workflows
- follow-ups
- useful task execution
- notifying when something matters

---

## 16. MEMORY SYSTEM

The system should have strong memory and automatically preserve useful context.

### 16.1 What it should remember
- project state
- prior conversations
- what the user is building
- user preferences
- how the user likes to communicate
- useful personal/project context
- what has already been done
- what failed and what worked

The user should not have to re-explain project context across chats.

### 16.2 Memory behavior
Memory should be:
- proactive
- mostly automatic
- managed by the system more than manually by the user

### 16.3 Memory layers
Sovereign should support multiple memory layers:

- Session memory
- Long-term user memory
- Operational memory (runs/failures/successes)
- Knowledge memory (docs, notes, retrieval)
- Secrets layer (separate from ordinary memory)

### 16.4 Credentials / secrets
Credentials should be securely handled.
The architecture should use secure storage/access patterns rather than plain conversational memory.

The system may:
- request credentials when needed
- remember that a credential exists
- retrieve them through a secure mechanism

It should not rely on raw credentials living loosely in ordinary conversation memory.

---

## 17. INTERFACE / UX MODEL

### 17.1 First interface
Slack should be the first interface.

### 17.2 Slack experience
Slack should feel primarily like:
- one DM conversation with the main CEO/operator

But the broader workspace may later allow:
- visibility into subagent conversations/workspaces
- structured views of work if desired

The user mainly wants to message one main operator.

### 17.3 Web dashboard
A web dashboard should come later and connect to the same system.

The dashboard should be:
- a hybrid view
- focused mainly on active work by default
- able to show reminders and history
- able to show what the system has done
- able to show memory/history
- able to show active subagents
- optionally later show logs/tool calls/live views

### 17.4 Live visibility
The user wants the ability to see:
- active subagents
- what is being worked on
- later maybe live/visual agent activity

This is valuable, but not required for v1.

### 17.5 Mid-task control
The user wants low required intervention.
They do not want the system to rely on them mid-task.
But they do want the ability to intervene if desired.

---

## 18. “ONE AI I TALK TO” REQUIREMENT

Sovereign should be designed so that the user mainly feels like they talk to one AI.

That main AI should:
- answer questions
- set reminders
- manage calendar/actions
- send emails/messages
- perform small tasks
- perform complex multi-step tasks
- build a team of agents when needed
- use tools behind the scenes

Other agents should mostly remain subordinate and optional to inspect.
The default user experience should be:
- one operator, many hidden workers

---

## 19. WHAT SUCCESS LOOKS LIKE

A successful Sovereign system should let the user:

- give a goal
- get asked minimal follow-up questions
- trust the system to plan intelligently
- trust it to spawn subagents
- trust it to use tools dynamically
- get real finished results
- rely on it for reminders, recurring tasks, email, and calendar support
- feel like they do not need to manage a bunch of separate AI tools manually

The “wow, this is real” feeling should come from:
- one operator
- full team behavior underneath
- strong autonomy
- real task completion
- strong review/verification
- useful assistant functions in daily life

---

## 20. MVP / ROADMAP DIRECTION

### Phase 1
- move repo local
- inspect current scaffold
- keep useful structure
- do not restart from zero

### Phase 2
- refactor toward LLM-driven orchestration
- introduce LangGraph supervisor/planning/execution/review pattern
- reduce hardcoded backend decision logic

### Phase 3
- implement first real operator loop:
  - Slack → Supervisor → Plan → Execute → Review → Result

### Phase 4
- add Supabase memory/persistence

### Phase 5
- add browser execution + evidence/review loop

### Phase 6
- add life-assistant layer:
  - reminders
  - recurring tasks
  - calendar
  - messaging/email

### Phase 7
- build web dashboard / operator console

### Phase 8
- add richer subagent visibility, more tools, and future capabilities

---

## 21. NON-NEGOTIABLE RULES

- Do not make the system primarily rule-based.
- Do not hardcode most planning/routing in Python.
- Do not rebuild mature tools unnecessarily.
- Do not pretend work is complete if it is not.
- Do not blindly trust executor agent outputs.
- Do not store raw credentials casually in ordinary memory.
- Do not make the user babysit the system constantly.
- Do optimize for:
  - goal completion
  - strong planning
  - dynamic subagent creation
  - review + verification
  - low-friction autonomy
  - strong memory
  - tool orchestration
  - one-AI user experience

---

## 22. FINAL SUMMARY

Project Sovereign is:

> A **goal-driven, LLM-orchestrated, multi-agent operator and life assistant** that acts like one main CEO-style AI while dynamically spawning a team of subagents and using external tools to complete real tasks, manage life operations, and return finished outcomes.

It should feel like:
- one AI to talk to
- one AI that understands goals
- one AI that can plan
- one AI that can delegate
- one AI that can review itself
- one AI that can act in daily life
- one AI that gets things done
