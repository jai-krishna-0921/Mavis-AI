# Owner decisions, 2026-10-08 (apply to the programs, connectors, multi-user and sandbox specs)

1. Ollama plan: **Pro** (3 concurrent; extra requests queue on Ollama's side). Budget for Pro; overflow to the secondary provider hook.
2. AWS resources: **approved** (IAM role + instance profile for the box, IMDS hop limit 2, S3 bucket(s) for workspaces/backups, daily EBS snapshots, $20/month budget alarm).
3. Sandbox per-user default: $5/month and 60 machine-minutes/day.
4. Browser v1: logged out only; logged-in sites later via Watch-live takeover.
5. Demo suite: on demand and after deploys; mirrored into the owner's chat tagged [test], no buttons.
6. Langfuse: on, sampled, user ids hashed. (Owner to confirm Ollama terms allow serving third-party users.)
7. Programs: plan and check-in messages do not count toward the 6/day ping budget; no mid-day nudges unless the user asks.
8. Health pack: habits only, no symptom/vitals logging in v1.
9. Grading: text only in v1 (no vision model).
10. Facts from the user's own records (own contacts, events they organised, own fitness data) count as trusted.
11. Disconnect: ask, with "forget everything from it" as the default.
12. WhatsApp: linked from Telegram first; WhatsApp-only signup later.
13. Access: invite codes. Server: t4g.medium (4 GB) as of 2026-10-08.

## Revenue intelligence (2026-10-08)
14. Primary CRM: **HubSpot** (sandbox/test portal to be provided by the owner; until then use HubSpot's developer test account).
15. Mavis serves **the owner's own company first** (one org); external customer orgs later.
16. Teams/Outlook: **off** until the owner confirms tenant-admin consent.
17. First org users: the owner is org owner; teammates join by org invite with roles admin/manager/rep/viewer. Managers see team-level summaries and risk flags; full transcripts only for meetings they attended or that the rep shares.
18. Market data: later phase, tracking/alerts/scenario analysis only, no predictions or advice.
