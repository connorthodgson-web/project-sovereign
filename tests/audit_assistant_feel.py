"""Deep audit script for assistant feel and reminder behavior."""

from __future__ import annotations

import time
from datetime import datetime

from core.assistant import AssistantLayer
from core.conversation import ConversationalHandler
from core.supervisor import Supervisor
from integrations.reminders.parsing import parse_one_time_reminder_request_with_fallback

# Test categories and inputs
TEST_CASES = {
    "greetings": [
        "hi",
        "hey",
        "hello",
        "yo",
        "good morning",
        "what's up",
        "hey there",
    ],
    "simple_reminders": [
        "remind me in 2 minutes to drink water",
        "remind me in ten minutes to stretch",
        "remind me tomorrow at 4 to check email",
        "remind me tonight to finish homework",
        "remind me next Friday at 3 PM to call mom",
    ],
    "weird_reminder_phrasings": [
        "can you ping me in a couple mins to drink water",
        "in like 2 minutes remind me about water",
        "don't let me forget to drink water in 2 mins",
        "tap me in a little bit to stretch",
        "remind me later to check my messages",
        "set something so I remember to drink water in two",
        "in 120 seconds remind me to stand up",
        "bug me in a sec to test something",
        "remind me after lunch to email him",
    ],
    "ambiguous_time_phrases": [
        "remind me later",
        "remind me soon",
        "remind me this afternoon",
        "remind me around 5",
        "remind me after school",
        "remind me early tomorrow",
        "remind me in a bit",
        "remind me this evening",
        "remind me next week",
    ],
    "invalid_edge_inputs": [
        "remind me yesterday to do something",
        "remind me 2 minutes ago to drink water",
        "remind me on February 30th",
        "remind me at 25:99",
        "remind me in -5 minutes",
        "remind me in zero minutes",
        "remind me every blargday",
        "remind me sometime",
        "remind me soonish",
    ],
    "multi_part_compound": [
        "remind me in 2 minutes to drink water and then in 5 minutes to take vitamins",
        "remind me tomorrow at 3 to email coach and ask if I finished math",
        "remind me in 10 minutes to check the server and make it brief",
        "hi can you remind me in 2 minutes to drink water",
    ],
}


def test_greeting_behavior():
    """Test whether greetings produce natural responses without internal state leakage."""
    print("\n" + "=" * 80)
    print("TEST CATEGORY: GREETINGS")
    print("=" * 80)
    
    assistant = AssistantLayer()
    supervisor = Supervisor(assistant_layer=assistant)
    
    results = []
    for greeting in TEST_CASES["greetings"]:
        print(f"\nTesting: {greeting!r}")
        start = time.perf_counter()
        
        # Test decision classification
        decision = assistant.decide_without_llm(greeting)
        print(f"  Decision mode: {decision.mode.value}")
        print(f"  Escalation: {decision.escalation_level.value}")
        print(f"  Should use tools: {decision.should_use_tools}")
        
        # Test full response
        response = supervisor.handle_user_goal(greeting)
        elapsed = time.perf_counter() - start
        
        print(f"  Response: {response.response!r}")
        print(f"  Latency: {elapsed * 1000:.1f}ms")
        
        # Check for robotic/internal phrasing
        issues = []
        response_lower = response.response.lower()
        
        robotic_phrases = [
            "planner",
            "router",
            "subtask",
            "orchestration",
            "execution loop",
            "scaffolded",
            "escalation",
            "objective_completion",
            "bounded_task_execution",
            "single_action",
            "conversational_advice",
        ]
        
        for phrase in robotic_phrases:
            if phrase in response_lower:
                issues.append(f"Contains internal jargon: {phrase!r}")
        
        if "i'm in the middle of" in response_lower and "build" in response_lower:
            issues.append("Leaking active task state into greeting")
        
        if issues:
            print(f"  ⚠ ISSUES: {', '.join(issues)}")
        else:
            print(f"  ✓ Response feels natural")
        
        results.append({
            "input": greeting,
            "response": response.response,
            "latency_ms": elapsed * 1000,
            "mode": decision.mode.value,
            "issues": issues,
        })
    
    return results


def test_reminder_parsing():
    """Test reminder parsing accuracy and failure modes."""
    print("\n" + "=" * 80)
    print("TEST CATEGORY: REMINDER PARSING")
    print("=" * 80)
    
    all_reminder_inputs = (
        TEST_CASES["simple_reminders"]
        + TEST_CASES["weird_reminder_phrasings"]
        + TEST_CASES["ambiguous_time_phrases"]
        + TEST_CASES["invalid_edge_inputs"]
    )
    
    results = []
    for reminder_text in all_reminder_inputs:
        print(f"\nTesting: {reminder_text!r}")
        
        outcome = parse_one_time_reminder_request_with_fallback(
            reminder_text,
            timezone_name="America/New_York",
        )
        
        if outcome.parsed:
            print(f"  ✓ Parsed successfully")
            print(f"    Summary: {outcome.parsed.summary!r}")
            print(f"    Deliver at: {outcome.parsed.deliver_at.isoformat()}")
            print(f"    Schedule phrase: {outcome.parsed.schedule_phrase!r}")
            print(f"    Parser: {outcome.parsed.parser}")
            print(f"    Confidence: {outcome.parsed.confidence}")
            
            # Check if time is in the future
            now = datetime.now(outcome.parsed.deliver_at.tzinfo)
            if outcome.parsed.deliver_at <= now:
                print(f"  ⚠ WARNING: Parsed time is not in the future!")
        else:
            print(f"  ✗ Parsing failed")
            if outcome.failure_reason:
                print(f"    Reason: {outcome.failure_reason}")
            print(f"    Attempted LLM fallback: {outcome.attempted_llm_fallback}")
        
        results.append({
            "input": reminder_text,
            "success": outcome.parsed is not None,
            "failure_reason": outcome.failure_reason,
            "summary": outcome.parsed.summary if outcome.parsed else None,
            "parser": outcome.parsed.parser if outcome.parsed else None,
        })
    
    return results


def test_reminder_latency():
    """Measure end-to-end latency for reminder requests."""
    print("\n" + "=" * 80)
    print("TEST CATEGORY: REMINDER LATENCY")
    print("=" * 80)
    
    supervisor = Supervisor()
    
    test_reminders = [
        "remind me in 5 minutes to drink water",
        "remind me in 10 mins to stretch",
        "remind me tomorrow at 3pm to check email",
    ]
    
    results = []
    for reminder_text in test_reminders:
        print(f"\nTesting: {reminder_text!r}")
        
        start = time.perf_counter()
        
        # Measure decision phase
        decision_start = time.perf_counter()
        decision = supervisor.assistant_layer.decide_without_llm(reminder_text)
        decision_time = time.perf_counter() - decision_start
        
        print(f"  Decision: {decision.mode.value} ({decision_time * 1000:.1f}ms)")
        
        # Note: We can't actually execute the full supervisor loop in this test
        # because it needs a Slack interaction context for reminders to work.
        # We're just measuring the decision overhead.
        
        results.append({
            "input": reminder_text,
            "decision_mode": decision.mode.value,
            "decision_latency_ms": decision_time * 1000,
        })
    
    return results


def test_compound_requests():
    """Test multi-part requests with mixed intents."""
    print("\n" + "=" * 80)
    print("TEST CATEGORY: COMPOUND REQUESTS")
    print("=" * 80)
    
    assistant = AssistantLayer()
    
    results = []
    for request in TEST_CASES["multi_part_compound"]:
        print(f"\nTesting: {request!r}")
        
        decision = assistant.decide_without_llm(request)
        print(f"  Decision: {decision.mode.value}")
        print(f"  Reasoning: {decision.reasoning}")
        
        # Check if it correctly identifies the reminder intent
        has_reminder = "remind" in request.lower()
        classified_as_action = decision.mode.value in ("act", "execute")
        
        if has_reminder and not classified_as_action:
            print(f"  ⚠ WARNING: Contains reminder but not classified as action")
        elif has_reminder and classified_as_action:
            print(f"  ✓ Correctly classified reminder intent")
        
        results.append({
            "input": request,
            "decision_mode": decision.mode.value,
            "has_reminder": has_reminder,
            "classified_correctly": has_reminder == classified_as_action,
        })
    
    return results


def main():
    """Run all audit tests and produce a summary report."""
    print("\n" + "=" * 80)
    print("PROJECT SOVEREIGN: ASSISTANT FEEL & REMINDER AUDIT")
    print("=" * 80)
    print(f"Started at: {datetime.now().isoformat()}")
    
    # Run test suites
    greeting_results = test_greeting_behavior()
    parsing_results = test_reminder_parsing()
    latency_results = test_reminder_latency()
    compound_results = test_compound_requests()
    
    # Summary statistics
    print("\n" + "=" * 80)
    print("SUMMARY STATISTICS")
    print("=" * 80)
    
    print(f"\n✓ Greetings tested: {len(greeting_results)}")
    greeting_issues = sum(1 for r in greeting_results if r["issues"])
    print(f"  Issues found: {greeting_issues}/{len(greeting_results)}")
    
    print(f"\n✓ Reminder inputs tested: {len(parsing_results)}")
    parsed_count = sum(1 for r in parsing_results if r["success"])
    print(f"  Successfully parsed: {parsed_count}/{len(parsing_results)}")
    print(f"  Failed to parse: {len(parsing_results) - parsed_count}/{len(parsing_results)}")
    
    print(f"\n✓ Compound requests tested: {len(compound_results)}")
    correct_classification = sum(1 for r in compound_results if r["classified_correctly"])
    print(f"  Correctly classified: {correct_classification}/{len(compound_results)}")
    
    print("\n" + "=" * 80)
    print("Audit complete!")
    print("=" * 80)


if __name__ == "__main__":
    main()
