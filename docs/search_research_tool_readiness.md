# Search / Research Tool Readiness

Project Sovereign keeps the same source-backed research layer under the CEO/operator:

CEO/Supervisor -> Research Agent -> SearchProvider -> provider implementation -> evidence -> reviewer/verifier.

## What It Does

The Research Agent uses the configured search provider for:

- current information questions
- tool, company, and product research
- comparison questions
- documentation lookup
- recent or news-like questions

Research is complete only when evidence includes:

- query
- provider
- answer or summary
- source titles and URLs
- timestamp

Simulated research and source-less research do not count as complete. If the provider returns an answer without grounding or usable source title/URL pairs, the Research Agent returns a blocked result and the reviewer/verifier gates must reject completion.

## Current Provider

Gemini is now the default provider implementation. It runs through OpenRouter, uses OpenRouter's web search server tool, and returns the same normalized `SearchResult` contract that the previous provider used:

- `query`
- `provider=gemini`
- `answer`
- `sources`
- `timestamp`
- `raw_metadata`

Perplexity was removed from the active provider path because it is not being used and adds unnecessary cost and setup complexity for the current MVP. OpenRouter is already part of the stack, so Gemini-backed search keeps the provider abstraction while reducing provider sprawl.

## Setup

Configure search through environment variables or the runtime secrets layer. Do not store API keys in ordinary memory.

```env
SEARCH_ENABLED=true
SEARCH_PROVIDER=gemini
OPENROUTER_API_KEY=your_openrouter_key
GEMINI_SEARCH_MODEL=google/gemini-2.5-flash
SEARCH_TIMEOUT_SECONDS=30
```

`OPENROUTER_API_KEY` is the required secret. `GEMINI_SEARCH_MODEL` is optional and defaults to the Gemini search model in app settings. If OpenRouter is unavailable or not configured, research returns a clean setup blocker instead of inventing an answer.

## Search vs Browser vs Browser Use

Use search when Sovereign needs to find information across sources and cite what it found. Search does not open, click, log into, test, or inspect a specific page.

Use browser execution when the user asks to open, inspect, summarize, or verify a specific URL, page, site, or UI state. Browser evidence stays on the Browser Agent path and remains reviewer/verifier gated. If a user wants to watch local browser work, enable visible browser mode through `BROWSER_HEADLESS=false`, `BROWSER_VISIBLE=true`, or `BROWSER_SHOW_WINDOW=true`.

Use Browser Use only for safe multi-step browser workflows when that backend is configured and the task needs more than direct page inspection. Browser Use does not replace the search provider and should not run for ordinary general research.

## Future Providers

Additional providers can be added behind the same `SearchProvider` contract if they are worth the cost and maintenance:

- Tavily for search API results
- Brave Search API for raw web search
- Exa for semantic web search
- Google Custom Search for constrained web lookup
- internal docs or vector search as a separate retrieval provider

Each provider must implement the same contract and must return usable source titles and URLs before research can be marked complete.
