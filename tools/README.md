# Tool Config Control Panel

This folder is the centralized setup surface for future integrations in Project Sovereign.

## How to use it

1. Open the tool category folder you want.
2. Paste your keys, tokens, emails, passwords, URLs, IDs, and setup values into `config.yaml`.
3. Set `enabled: true` for tools you want available later.
4. Update `status` from `not_configured` to `configured` or `ready`.
5. Add any provider-specific reminders in `notes.md`.
6. Mirror the current state in `TOOL_REGISTRY.yaml` so you can see readiness at a glance.

## What each folder is for

- `auth/`: shared login credentials, OAuth app settings, callback URLs, shared identities.
- `browser/`: browser automation setup, sessions, Playwright defaults, proxy and storage state.
- `calendar/`: Google or Microsoft calendar credentials and sync settings.
- `coding/`: GitHub, Git providers, package registries, CI/CD, and repo access details.
- `email/`: SMTP and email API providers, sender identities, app passwords, and webhook values.
- `files/`: local and cloud file system locations, upload targets, and storage credentials.
- `llm/`: model providers, API keys, model routing defaults, and fallback model settings.
- `memory/`: Supabase, vector databases, embeddings, and persistence settings.
- `messaging/`: SMS, Slack, Discord, Telegram, WhatsApp, and webhook-based messaging.
- `misc/`: catch-all external services that do not fit cleanly elsewhere.
- `search/`: search APIs, scraping/search providers, and SERP access.
- `voice/`: voice agents, telephony, speech-to-text, text-to-speech, and call providers.

## Recommended setup order

1. `llm/`
2. `auth/`
3. `browser/`
4. `email/`
5. `messaging/`
6. `calendar/`
7. `memory/`
8. `coding/`
9. `files/`
10. `search/`
11. `voice/`
12. `misc/`

## Status suggestions

- `not_configured`: nothing pasted yet
- `partial`: some values are filled in
- `configured`: credentials are present
- `ready`: intended to be usable when integrations are wired up
- `paused`: configured, but intentionally disabled

Keep everything centralized here so future integration work can plug into one control panel instead of hunting through the repo.
