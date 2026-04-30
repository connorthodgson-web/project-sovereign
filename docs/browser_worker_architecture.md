# Browser Worker Architecture

Project Sovereign keeps browser work under the main CEO/Supervisor path while separating three different jobs:

1. **Quick research/search** answers simple current-info or factual questions through `SearchProvider` and Gemini/OpenRouter search.
2. **Research Agent** handles deeper source-backed research tasks, synthesis, and citation evidence. It does not open or interact with a specific page.
3. **Browser Agent** handles specific URL/page inspection through the shared `browser_tool` and local Playwright path.
4. **Browser Use provider** is optional and reserved for safe multi-step public browser workflows when enabled.

All browser outputs must normalize to evidence before they can be reviewed, verified, or shown as complete.

## Responsibilities

### Search And Research

Use SearchProvider/Gemini for:

- simple factual or current-info questions
- broad research across sources
- documentation lookup
- comparison or tool/product research

Research evidence must include the query, provider, answer, source titles, source URLs, and timestamp. Source-less research remains blocked.

### Browser Agent And Playwright

Use Browser Agent with Playwright for:

- opening a specific URL
- inspecting a known page
- summarizing visible page content
- collecting screenshots or structured page evidence
- confirming a page loads

Playwright is the direct local page-inspection path. It should stay deterministic, bounded, and evidence-first.

### Browser Use Provider

Use Browser Use only when:

- `BROWSER_ENABLED=true`
- `BROWSER_USE_ENABLED=true`
- provider credentials and SDK are available
- the task is a safe multi-step public browser workflow

Browser Use does not replace SearchProvider for research and does not replace Playwright for direct URL inspection.

## Visible Local Browser Setup

Default browser execution remains headless.

Use one of these settings to make local browser work visible:

```env
BROWSER_ENABLED=true
BROWSER_HEADLESS=false
```

or:

```env
BROWSER_ENABLED=true
BROWSER_VISIBLE=true
```

or:

```env
BROWSER_ENABLED=true
BROWSER_SHOW_WINDOW=true
```

When visible mode is enabled, the Playwright adapter launches a local browser window with `headless=false`. Evidence capture still works, and screenshots still follow `BROWSER_SAVE_SCREENSHOTS`.

## Human-In-The-Loop Auth Continuation

Sovereign must not automate:

- login or credential entry
- CAPTCHA or human verification
- 2FA, OTP, or verification-code steps
- payments, checkout, purchases, or billing forms
- sensitive medical, tax, banking, SSN, or credential forms
- graded schoolwork submission or completion

If a browser page hits login, CAPTCHA, or 2FA, Sovereign should:

- return a blocked result, not success
- ask the user to complete the step manually in the visible browser when available
- store a short-term pending browser continuation state
- accept a follow-up like `continue` to retry inspection when feasible
- avoid storing passwords, tokens, cookies, or verification codes in ordinary memory

Current continuation is best-effort: it retries the original browser inspection after the user says `continue`. Long-lived browser-session handoff is not fully implemented yet.

## Evidence And Review

Successful browser evidence should include:

- requested URL
- final URL
- visited URLs when available
- title
- headings or visible text preview
- summary text or extracted result
- status code when available
- screenshot path when available
- backend used
- blocker metadata if blocked

Reviewer and verifier gates still reject weak browser evidence. A generic claim that a browser task succeeded is not enough.

## Future Fallback Plan

The intended fallback order is:

1. SearchProvider for source-backed research.
2. Playwright for known URL inspection.
3. Browser Use for safe multi-step browser workflows.
4. Future remote browser or managed provider only through the same adapter/evidence contract.

Remote browser execution should preserve the same safety policy, evidence shape, reviewer gates, and no-credential-storage rule.

## Not Implemented Yet

- Persistent visible browser session handoff after login/CAPTCHA/2FA.
- A dashboard view of live browser windows or artifact history.
- Remote browser fallback.
- Browser Use provider-specific local window control beyond the shared visible-mode request metadata.
- Credential vault integration for secure auth workflows.
