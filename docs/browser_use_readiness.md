# Browser Use Readiness

## Setup

Browser Use is an optional provider under the existing Browser Agent. It does not replace the local Playwright path.

Required runtime pieces:

- `BROWSER_ENABLED=true`
- `BROWSER_USE_ENABLED=true`
- `BROWSER_USE_API_KEY=<provider key>`
- Browser Use SDK installed in the Python environment
- optional `BROWSER_BASE_URL=<provider endpoint>` when using a non-default Browser Use endpoint

`BROWSER_BACKEND_MODE=auto` is the preferred mode. In auto mode, direct URL checks stay on Playwright while safe multi-step browser objectives can select Browser Use.

## Env Vars

- `BROWSER_ENABLED`: master switch for browser execution.
- `BROWSER_BACKEND_MODE`: `auto`, `playwright`, or `browser_use`.
- `BROWSER_HEADLESS`: set `false` to show the local browser window when the active provider supports local visible execution.
- `BROWSER_VISIBLE` / `BROWSER_SHOW_WINDOW`: clearer aliases for visible local browser execution.
- `BROWSER_SAVE_SCREENSHOTS`: `never`, `on_failure`, or `always`.
- `BROWSER_USE_ENABLED`: enables Browser Use as an optional provider.
- `BROWSER_USE_API_KEY`: credential for the Browser Use provider. Store it in environment or a secrets layer, not conversation memory.
- `BROWSER_BASE_URL`: optional Browser Use-compatible endpoint.

There is no separate `BROWSER_USE_MODEL` setting in the current codebase. If the SDK/provider later requires one, add it behind the same adapter boundary.

## What Browser Use Is For

Browser Use is for safe multi-step browser workflows where a direct page load is not enough, such as navigating a public site, following safe links, or collecting evidence across a short workflow. It is optional and local-first where provider support exists; otherwise it must still return normalized evidence through the browser adapter boundary.

It is not used for simple URL inspection, page title checks, or ordinary summaries. Those continue through the local Playwright browser tool because it is deterministic, cheap, and already evidence-gated.

## Capability Differences

- Search: finds and summarizes source-backed information from a search provider. It does not open or interact with a specific page.
- Playwright: opens a specific URL locally, extracts title/headings/text/status, and captures screenshots when policy requires them.
- Browser Use: optional managed multi-step browser worker for safe exploratory workflows, normalized into the same browser evidence shape.
- Future Manus/OpenAI computer-use: not added in this pass. They should plug in later through adapter contracts and the same reviewer/verifier evidence gates.

## Safety Policy

Browser Use must not automate:

- credential entry, login, password, or account-auth flows
- CAPTCHA or human-verification steps
- 2FA or verification-code steps
- purchases, payments, checkout, billing, or credit-card forms
- sensitive personal, medical, tax, or banking forms
- school-portal submissions or completion work unless explicitly safe and the user is present for auth/submission

Unsafe tasks return a plain blocked state with the smallest next action needed. Credentials must never be stored in ordinary memory.

## Evidence Policy

Browser Use results must normalize into the shared browser evidence shape:

- requested goal
- requested URL when available
- visited URLs and final URL
- extracted result, summary text, visible text preview, title, and headings
- screenshots or artifacts when available
- blockers, errors, and user action requirements

Reviewer/verifier gates still require actual browser evidence. A generic "agent says done" result is not enough.

## Remaining Gaps

- Real Browser Use SDK behavior needs live credential testing in a controlled environment.
- Provider-specific model settings are not wired because the current scaffold only expects API/base URL settings.
- More nuanced LLM-side provider selection can be added later, but the Python boundary should remain a safety and adapter layer, not the main planner.
- Live browser evidence should eventually include richer artifact indexing for dashboard visibility.
