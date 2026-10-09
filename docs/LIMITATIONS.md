# Mavis AI: current limitations (2026-10-09)

What Mavis can and cannot do today, what it costs, and what would lift each limit. Updated as things ship.

## 1. AI model (Ollama Cloud Pro)

| Item | Limit today | Notes |
|---|---|---|
| Concurrent model calls | 3 at a time, shared by every user | This is 3 calls, not 3 people. Extra calls wait in a queue. A chat reply takes 1 to 3 calls of a few seconds each, so 10 to 20 light users work; at peak, replies slow down. |
| Background work | Gets a guaranteed share but never blocks chat | Memory learning from mail and Slack queues behind chat when busy and catches up later. |
| Cost | Fixed monthly plan | The main cost driver as users grow. |
| Lift it | Higher Ollama plan, or a second model provider for background work | Needed past roughly 20 active users. |

## 2. Google Workspace (in-house connector)

| Item | Limit today | Notes |
|---|---|---|
| Google Cloud project | Required, free, no billing account | It only registers Mavis with Google so Google will issue tokens. Nothing runs there. Mavis runs on AWS. |
| API cost | Free | Gmail, Calendar, Drive, Docs, Sheets, People, Tasks, Meet APIs are free within per-user quotas a personal assistant stays far below. |
| Users | 100 people, ever (lifetime cap for the project) | Applies while the app is unverified. Each person sees a one-time "Google hasn't verified this app" screen. |
| Lift the cap | Google verification | Gmail and full Drive are "restricted" scopes: verification needs an annual paid third-party security assessment and takes weeks. |
| Company mail | kripya.com is on Microsoft 365, not Google | Outlook and Teams are not connected (deferred by owner decision). |
| New mail delay | Up to about 2 minutes | Gmail is polled, not pushed. Push (Pub/Sub) can come later if needed. |
| First sync | Last 14 days of mail, calendar 7 days back to 30 ahead | Older data is searchable on demand, not pre-learned. |
| Writes | Docs, Sheets, Drive, Tasks, Meet writes moving in house today | Sharing, commenting on others' files, sending mail and inviting guests always ask first. |

## 3. Slack (in-house connector)

| Item | Limit today | Notes |
|---|---|---|
| API cost | Free | Internal app. |
| Workspaces | Your workspace only | Everyone in it can connect their own Slack. Other companies' workspaces cannot. |
| Lift it | Distribute the app | Distributed apps outside the Slack Marketplace are capped at 1 history call per minute and 15 messages, so in practice that needs Marketplace approval. |
| Chat with Mavis in Slack | Being built today | DM or @mention Mavis; approvals and progress as Slack buttons. |
| First sync | Last 7 days, up to 200 messages per channel, 2000 total | DMs first. Thread replies are not backfilled. |
| Sends | Always one tap to approve | Inbound messages can carry injected instructions. |

## 4. Who can use Mavis

| Item | Limit today | Notes |
|---|---|---|
| Access | Only allowlisted Telegram chats | Adding a person is a config change and a redeploy. |
| Invite codes | Built, not merged | On the parked multi-user branch. About half a day to merge and deploy. |

## 5. The machine (code and browser for the agent)

| Item | State | Notes |
|---|---|---|
| Visible progress (Slice A) | Live | Progress cards, Cancel button, file delivery. |
| Code machine (Slice B) | Not built | Mavis cannot write and run code, do data analysis on files, or build decks and spreadsheets in a sandbox yet. |
| Browser (Slice C) | Not built | Mavis cannot operate websites (forms, logins, bookings). It can search and read web pages. |
| Watch live, usage digest (Slice D) | Not built | |
| Cost when built | About $2 a month for the owner, about $16 a month for 10 active users | AWS AgentCore, billed only while code runs. Per-user caps: 60 machine minutes a day, $5 a month. |
| AWS resources | Already approved by the owner | IAM role, S3 bucket, budget alarm. |

## 6. Server (AWS)

| Item | Limit today | Notes |
|---|---|---|
| Hosting | One EC2 t4g.medium (2 vCPU, 4 GB) in ap-south-1 | No redundancy: if the box is down, Mavis is down. Fine for tens of users. |
| Address | `13-233-241-141.sslip.io` | A free wildcard DNS name. A real domain is better long term and may be required if Google rejects this redirect address. |

## 7. Telegram

| Item | Limit |
|---|---|
| Files Mavis sends | 50 MB each (larger files are named, not attached) |
| Files you send Mavis | 20 MB each |
| Message edits | At most one progress card edit every 8 seconds per chat |

## 8. Safety rules that are deliberate limits

- Outward actions (send email, post in Slack, invite guests, share files) always need your tap, because mail and Slack content can contain hidden instructions.
- Mail and Slack content is always treated as third-party data: it can inform Mavis but never instruct it, and it never becomes a "fact you told Mavis".
- Secrets (OTPs, passwords, API keys, card, bank, Aadhaar, PAN numbers) are masked before anything is stored. Masking is best effort, not a guarantee.
- A Google or Slack consent link is valid for 10 minutes and works once. Do not forward it: whoever consents links their account to your Mavis.

## 9. Not built yet (parked by owner decision)

- Revenue intelligence (HubSpot, Salesforce, Fathom, org roles).
- Commitments ledger, coaching programs, the generic connector framework.
- Microsoft 365 (Outlook, Teams, OneDrive).
- WhatsApp, Instagram (need Meta app approval), LinkedIn (no usable API for personal data).
- Stock market data: analysis and alerts only, never predictions.
