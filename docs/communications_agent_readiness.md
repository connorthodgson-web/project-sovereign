# Communications Agent Readiness

## Current Gmail Architecture

Project Sovereign keeps one user-facing CEO/operator. Email work sits underneath that voice:

CEO / Supervisor -> Personal Ops / Communications Agent -> Contacts provider/memory + Gmail provider/tools.

The Communications Agent owns Gmail read/search/summarize/draft behavior plus guarded outbound and mailbox-changing actions. Named recipients resolve through a small contacts layer before Gmail is called. The Gmail client is a thin provider adapter: it handles OAuth readiness, normalized Gmail messages, drafts, sends, and mailbox operations without returning credential contents. Python provides adapter boundaries, confirmation state, contact storage, short-term follow-ups, and evidence. It should not become the main planning brain.

## Supported Commands

Read and search:
- "do I have any emails from teacher@example.com?"
- "search my email for tuition receipt"
- "find emails from school"
- "summarize unread emails"
- "summarize recent important emails"

Draft:
- "draft an email to teacher@example.com saying thanks"
- "create an email draft to alex@example.com with subject Homework saying I finished it"
- "reply to the latest email from teacher@example.com saying got it"
- "draft an email to Mom saying thanks" after Mom has been explicitly saved as a contact

Send:
- "send an email to parent@example.com saying I arrived"
- "send Mom an email saying I arrived" after Mom has been explicitly saved as a contact

Contact memory:
- "Mom's email is mom@example.com"
- "Remember that Mom's email is mom@example.com"
- "Alex is alex@example.com"
- "Use dad@example.com for Dad"
- "Mom's email changed to new-mom@example.com"

Mailbox changes:
- Archive, trash, delete, forward, and bulk-style requests are treated as guarded or unsupported. Archive/trash/delete require confirmation. Forwarding is staged but remains blocked after confirmation until a safe forwarding construction path is implemented.

## Confirmation Policy

No confirmation needed:
- Search/read Gmail.
- Summarize Gmail results.
- Create Gmail drafts.
- Create reply drafts.

Confirmation required:
- Send email.
- Send an existing draft.
- Archive email.
- Trash email.
- Permanently delete email.
- Any future forwarding or bulk mailbox action.

Unsupported or blocked:
- Forwarding is not fully implemented yet.
- Broad bulk mailbox actions are capped and should stay conservative.
- Unknown recipient names ask one concise follow-up for an email address instead of guessing.
- Ambiguous contact matches ask the user to choose instead of guessing.

## Contact Memory Behavior

Contacts are stored in the structured Personal Ops JSON store, separate from ordinary semantic facts and project memory. The store supports alias/name/email records, exact lookup by alias or email, partial lookup for user-facing disambiguation, and explicit updates when the user says an email changed.

Sovereign only saves a contact when the user clearly provides both the alias/name and email address. Safe examples include "Mom's email is mom@example.com", "Alex is alex@example.com", and "Use dad@example.com for Dad." It does not scrape contacts, import address books, infer relationships, or store raw contact books in semantic memory.

Recipient resolution is conservative:
- Exact alias or email match resolves to the saved email.
- Multiple saved matches ask which contact to use.
- Unknown names ask for the email address.
- Drafts to saved contacts can be created without confirmation.
- Sends to saved contacts still require explicit confirmation.

## Setup Requirements

Gmail requires:
- Gmail enabled in settings.
- Google Gmail OAuth credentials file present.
- Saved Gmail OAuth token/access file present.
- Google Gmail Python dependencies installed.

If Gmail is not connected, the user-facing reply should say plainly that Gmail setup is needed and that Gmail must be enabled with OAuth credentials and saved Gmail access. Raw token values, credential contents, and secret-like data must never be stored in ordinary memory or echoed in replies.

Contact aliases are allowed because the user explicitly provides them, but tokens, passwords, API keys, OAuth values, and client secrets must never be saved as contacts. Contact data should only appear in replies when it is directly relevant to the user's current contact/email request.

## Remaining Gaps

- Reply drafting uses the latest matching Gmail result from search; richer thread selection and disambiguation should come later.
- Forwarding needs a dedicated safe message construction path before execution.
- Search query construction is intentionally lightweight until an LLM-led communications planner/tool call layer is attached.
- Gmail labels, attachments, and rich HTML bodies are not deeply modeled yet.
- Contacts are local structured memory only for now; there is no Google Contacts sync, address-book import, phone/SMS resolution, or automatic relationship discovery.
- Ambiguous contact follow-up currently expects a clear contact name or email address; richer numbered-option selection can come later.

## What Should Come Next

Next pass: improve reply/thread disambiguation, add richer contact-choice continuations, and introduce a stronger LLM-led Communications Agent planning prompt that selects Gmail tools dynamically while preserving the same confirmation gates.
