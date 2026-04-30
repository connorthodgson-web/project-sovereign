# Browser Execution Readiness

## Current Architecture

Browser execution is a bounded local capability under the CEO/operator path:

1. `core.browser_requests` detects obvious URL-based browser asks and normalizes Slack-style links.
2. `core.assistant` and `core.fast_actions` route simple browser requests to `browser_agent` without letting an LLM answer from memory.
3. `agents/browser_agent.py` resolves the target, builds a `browser_tool` invocation, executes at most a small number of browser actions, synthesizes only from captured evidence, and returns blocked when evidence is weak.
4. `tools/browser_tool.py` normalizes the URL and calls `BrowserExecutionService`.
5. `integrations/browser/runtime.py` uses the local Playwright adapter for direct URL inspection and keeps Browser Use as an optional provider for safe multi-step workflows when explicitly enabled.
6. `agents/reviewer_agent.py` and `core/evaluator.py` require concrete browser evidence before browser work can be treated as complete.

## Supported Safe Commands

The safe local browser path supports direct, low-risk page inspection:

- check or open a URL
- summarize a public page
- tell me the page title or visible heading
- inspect a URL and return a short report
- verify that a page loads

These tasks should produce structured evidence rather than a bare text claim.

## Blocked Or High-Risk Commands

Sovereign must not claim success for:

- missing or invalid URLs
- missing or disabled local browser access
- login walls or authenticated pages
- CAPTCHA or human-verification gates
- 2FA or verification-code flows
- payment, purchase, checkout, billing, or credit-card steps
- sensitive forms such as SSN, banking, medical, tax, or credential entry
- access denied, unauthorized, forbidden, 401, 403, or auth-wall pages

The user-facing response should explain the blocker plainly and name the smallest next action, without leaking adapter or runtime internals unless setup detail is explicitly useful.

## Evidence Policy

A successful browser result should include, when available:

- requested URL
- final URL
- page title
- headings
- visible text preview or summary text
- HTTP status code
- screenshot path

Reviewer and evaluator gates require at least a final URL plus visible page content, or a final URL plus a clear page title. A generic summary with no page evidence is rejected. Blocked results remain blocked even when the browser captured partial page content.

Screenshots are helpful evidence, especially for blocked pages, but they are not mandatory for ordinary successful page summaries when structured page evidence is present.

## Setup Requirements

For local browser execution:

- `BROWSER_ENABLED=true`
- `BROWSER_HEADLESS=false`, `BROWSER_VISIBLE=true`, or `BROWSER_SHOW_WINDOW=true` for a visible local browser window
- Playwright Python package installed
- Playwright Chromium installed
- `BROWSER_SAVE_SCREENSHOTS` set to `never`, `on_failure`, or `always`

Screenshots are stored under `.sovereign/browser_artifacts` inside the configured workspace root, not in user-created artifact folders.

When login, CAPTCHA, or 2FA blocks visible browser work, Sovereign stores short-term continuation state and asks the user to complete the step manually, then say `continue`. It does not store passwords, tokens, cookies, or verification codes in ordinary memory.

## Optional Adapter Path

Browser Use now plugs into the existing browser contract as an optional provider. Future OpenAI computer-use, Manus, and OpenClaw adapters should return the same normalized evidence shape:

- requested/final URL
- title/headings/text preview
- status or blocker metadata
- screenshot path when available
- user action requirements for blocked flows

Any future managed browser adapter must preserve the same reviewer/verifier rules: no evidence, no completion.
