# Browser Notes

Use this folder for browser automation defaults and site session prep.

Useful things to keep here:
- login credentials for browser-only portals
- proxy details
- session persistence file locations
- headless preferences
- Playwright launch settings

Common future uses:
- school/admin sites
- Playwright
- browser-use
- cookie/session reuse

Current live path:
- direct URL opening/inspection through the local Playwright runtime
- page title extraction
- concise page summary text
- screenshot evidence saved under `workspace/created_items/browser/`

USER ACTION REQUIRED when browser execution is not ready:
- set `BROWSER_ENABLED=true`
- install the Playwright package if it is missing
- run `python -m playwright install chromium` if the Chromium binary is missing

Optional next-stage setup:
- add `BROWSER_USE_API_KEY` to enable the Browser Use cloud adapter for richer future browser-agent tasks
