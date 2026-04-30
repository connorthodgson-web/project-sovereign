# Evidence Expectations

Completion should be tied to evidence, not tone.

Evidence rules:
- live workspace/file actions should return concrete file or runtime evidence
- browser work should aim for screenshots, structured results, or explicit confirmations
- communications work should aim for delivery confirmation and message metadata
- calendar and reminder work should aim for event/reminder ids or confirmation metadata
- research/search work must surface query, provider, answer/summary, source titles/URLs, and timestamp
- retrieval work should surface backend/source information where possible

If the system cannot produce the expected evidence:
- do not mark the work complete
- describe the result as blocked, scaffolded, simulated, or planned
- state the minimum next action needed to make the capability real
