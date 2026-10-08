# Mavis AI: Programs (proactive personalised coaching) Design (Track 2)

Date: 2026-10-08
Status: draft for owner review (no code written)
Base: main 59715fe (hotfix4 merged). Depends on: Phase B commitments ledger (branch `ledger`, spec
`2026-10-03-mavis-commitments-ledger-design.md`), Track 1 register signal (T1.2) and reactions (T1.3).
Feeds from: Track 3 connectors (calendar, fitness, finance records). Extended by: Track 4 sandbox.
Research input: scratchpad `research-coaching.md` (pointers re-verified against main in section 15).

## 1. Problem, goals, non-goals

### 1.1 Problem
The owner wants Mavis to coach toward a goal the way "Instinct" does: a short intake, then every day a
numbered plan ("Day 5") at a time Mavis promised, with time-boxed blocks and real resources, an evening
check-in that counts even one finished task, work that is collected and corrected, difficulty that adapts,
a lighter fallback for bad days, and a casual tone that mirrors the user.

Mavis has none of the parts. There is one learned morning check-in (`initiative/routines.py`), an
inbox-only evening wrap that is skipped on quiet days (`attention/rhythm.py`), no multi-day plan, no
assignments, no grading, no difficulty state, and no promised delivery time. The proactive rules from
hotfix4 (one ping per subject per day, subject-bound wakeups, no self-feeding reasoner) would also block a
naive "send a plan and a check-in" design.

### 1.2 Goals
- G1. A domain-agnostic **Program** engine: intake, daily session, check-in, submissions, grading,
  adaptation, pause and resume. Domain behaviour (education, fitness and health habits, finance) is
  **data in packs**, never `if domain == ...` in code.
- G2. A promised time is kept: the plan arrives at the time Mavis said, even when the LLM is slow, because
  it is generated the night before and the send only renders stored data.
- G3. Every resource link in a plan was fetched successfully in the run that produced the plan. Code renders
  links; no model writes a URL.
- G4. Every progress claim ("you cleared the full list yesterday") is computed by code from logged data.
- G5. Programs are good citizens of the proactive system: at most two promised messages a day per user
  (morning plan, evening check-in) however many programs run, merged with the existing morning brief and
  evening wrap, inside quiet hours rules.
- G6. Programs use the commitments ledger for everything a user can be told is pending. No second pending
  store.
- G7. Health and finance safety rules are short, deterministic where it matters, and do not make Mavis
  preachy.
- G8. Per user everything (multi-user ready): every row, cap, schedule and budget is per user.

### 1.3 Non-goals (this track)
- Medical, diagnostic or clinical programs; medication management; investment advice of any kind.
- A web or app UI. Telegram only.
- Group programs, leaderboards, social features.
- Building connectors (Track 3) or the sandbox (Track 4). This spec only defines the inputs and hooks.
- Re-specifying register mirroring or reactions: Track 1 owns them; Programs consume them (section 11.5).
- Payment, subscriptions or paid content.

## 2. User journeys

Copy below is illustrative, in Mavis's voice, with no em or en dashes. Plan bubbles are rendered by code.

### 2.1 Intake (3 to 4 questions, one message each, conversational)
Triggered when the user states a goal with a horizon ("help me prep GATE CS and GRE by Feb", "I want to get
back to running", "help me stop overspending"). The chat agent, given the pack catalogue as context, calls
`program_propose` once it has the answers. Questions come from the matched pack's `intake` list, asked only
for fields still unknown (the model already knows the goal and maybe the deadline from the first message):

1. Goal and deadline (often already given).
2. Current level ("roughly where are you with OS and DBMS?"; optional one-item diagnostic from the pack).
3. Time per day and the usual window ("about 2 hours, mornings before work").
4. When the plan should land and when to check in ("8 for the plan, 9:30 pm check-in work?").

Pack-specific safety intake (fitness: "any injuries or conditions I should plan around?") is one question,
asked once. Then one confirming message, and the program is created:

> Locked in. 90 days, GATE CS plus GRE, about 2 hours a day. Day 1 lands tomorrow at 8, and I'll check in
> at 9:30 pm. Say "lighter" or "pause" anytime.

Rules: never more than 4 intake questions; ambiguous domain gets one clarifying question; times inside
quiet hours are not accepted (Mavis offers the nearest allowed time, section 5.4); a fourth concurrent
program is refused with an offer to pause one.

### 2.2 Daily plan (the morning message)
At the promised time, one message: a composed bubble (voice, computed rationale) plus a code-rendered plan
bubble. If the learned morning check-in falls within 90 minutes, it is merged in (section 5.2), so the
calendar and inbox lines ride along and there is no second morning ping.

> Morning! Day 5. You cleared the whole list yesterday, so same size today.

```
Day 5 (Thu), about 2 h
8:00 to 8:45  GATE OS: paging. NPTEL lecture 23, watch 12:30 to 25:00
              (link to the lecture page, as fetched)
8:45 to 9:15  GRE quant: 10 problems, ETS sample set, section 2
9:15 to 9:30  English: 5 sentences with today's words. Send them to me
9:30 to 9:40  Recall: 12 cards
Short on time? Just GATE and English, 60 min.
Also today: dentist 4 pm, 2 emails waiting on you.
```

Blocks are anchored to the study window from intake; without a window they show durations only.

### 2.3 Submission and feedback
The user sends work in chat ("here are my sentences: ..."), or a photo of handwritten work. A context
provider has already told the chat agent which items are open and collectable, so it calls
`program_submit(item_id, content)` without any keyword parsing. The tool grades (section 7) and returns a
result the chat model turns into a short reply:

> 4 of 5 are clean. #3: "He suggested me to go" should be "He suggested that I go" ("suggest" doesn't take
> a person as object). Nice use of "ubiquitous".

The ledger row for that assignment closes with submission evidence. Missed concepts become review cards.

### 2.4 Evening check-in
At the check-in time, only if today's session was delivered, one message with computed state and buttons
per program (`Did it all` / `Some of it` / `Not today`):

> Hey, how'd Day 5 go? You already sent the sentences. Even one task counts.

`Some of it` asks which, in chat. Free text replies work too ("did GATE, skipped GRE, too tired"). If the
attention evening wrap is due within 90 minutes, its inbox lines are merged in (section 5.2).

### 2.5 Adaptation
After each check-in, code updates each track's load and difficulty (section 8) and writes a one-line
rationale for tomorrow's composer. Explicit requests ("too much", "make it harder", "no GRE this week") go
through `program_adjust` and apply from the next session (or today, if today's plan is not yet done:
"lighter" switches today to the lite subset at once).

### 2.6 Pause, resume, ignore
- "Pause GRE till Monday": `program_adjust(pause, until)`. Wakeups for that program are cancelled, the
  day counter does not advance, open assignments are carried into the first session after resume.
- Ignored (no reply, no logs): after 2 consecutive no-signal days, the next plan is the lite subset plus one
  line: "Want me to keep sending these, or pause for a bit?" After 3, the program auto-pauses, Mavis says so
  once, and sends nothing for it until the user writes. When they next write, a context line tells the chat
  agent the program is paused so it can offer to resume; resume needs the user's words (tool call).
- Never guilt copy. No "you broke your streak". The counter is "days showed up", and missed days are frozen,
  never reset.

### 2.7 Multiple programs and the daily message budget
- At most 3 active programs per user (setting).
- All programs share one morning message and one evening check-in, with a section per program. The
  user's declared daily minutes are one budget split across programs by weight, so a second program never
  silently doubles the day.
- A program may have its own time only if the user asks for it ("send the workout at 6 pm"); that is then a
  separate promised message and the only way to exceed two program messages a day.
- Promised program messages pass the daily budget gate (they are user-requested, like `wake_me`
  reminders) and are counted in it, so they reduce what unsolicited pings may still send that day. See
  open question 1.
- Feedback on submissions and answers to check-ins are replies, not proactive messages.
- Weekly recap (every 7th session) is part of that evening's check-in, not an extra message.

## 3. Concepts

- **Pack**: versioned data describing a domain (section 9).
- **Program**: one user goal with a horizon, created from a pack. 1 to 4 **tracks** (GATE, GRE verbal,
  GRE quant, English; or run, strength, sleep).
- **Session (Day N)**: one dated plan per local day for a program. N counts delivered sessions, so pauses do
  not advance it.
- **Item**: an atomic task in a session: `kind` (from the pack), minutes, difficulty, optional resource,
  optional `collect` spec, optional rubric or answer key.
- **Lite subset**: item ids from the session that make a smaller valid day.
- **Submission**: the user's response to a collectable item, with its grade.
- **Review card**: a spaced-repetition fact derived from items and misses.
- **Resource**: a fetched, verified page, video or file, recorded per generation run.

## 4. Data model

Migration number: the ledger branch carries `0013_commitments`, which collides with main's
`0013_task_outcomes`; it will be renumbered when it merges. Programs take the next free number after the
ledger lands (expected `0015_programs`). Every table has `user_id` (indexed) and every repo query filters
by it.

```
program_packs_seen(pack_id, version, content_hash, loaded_at)        -- audit of which pack data was live

programs(id, user_id, pack_id, pack_version, title, goal_text,
         starts_on, ends_on null, status [active|paused|done|dropped],
         paused_until null, pause_reason [user|ignored|safety] null,
         session_time 'HH:MM', checkin_time 'HH:MM', own_time bool,      -- own_time: separate message
         days_of_week int bitmask, daily_minutes int, weight float,
         study_window_start 'HH:MM' null,
         days_showed_up int, sessions_delivered int, no_signal_streak int,
         settings JSON,                                                  -- per-user overrides of pack defaults
         intake JSON,                                                    -- answers, as given
         ledger_id null,                                                 -- the GOAL commitment
         provenance [user|third_party],                                  -- from the intake turn's taint
         created_at, updated_at, version)
  unique (user_id, title) where status in (active, paused)

program_tracks(id, user_id, program_id, name, topic_ref, level float, load_minutes int, weight float,
               state JSON)                                               -- topic cursor, recent c/a values

program_runs(id, user_id, program_id, local_date, kind [generate|regenerate|fallback],
             status [queued|running|ok|failed|fallback], attempts int, started_at, finished_at,
             model, error_code null)

program_resources(id, user_id, run_id, program_id, url, canonical_url, title, kind [page|video|pdf|artifact],
                  duration_s null, http_status, fetched_at, content_hash, domain_class [pack|open],
                  excerpt text)                                          -- what the coach saw; untrusted

program_sessions(id, user_id, program_id, day_number, local_date,
                 status [planned|sent|partial|done|lite|skipped|no_signal|paused],
                 run_id, plan JSON,                                      -- validated SessionPlan
                 lite_item_ids JSON, rationale text,                     -- code-written
                 sent_at null, checkin_sent_at null, closed_at null,
                 completion float null, accuracy float null)
  unique (program_id, local_date)

program_items(id, user_id, session_id, track_id, ord, kind, title, minutes, difficulty float,
              resource_id null -> program_resources, segment JSON null,  -- {from_s, to_s}, only with duration
              collect JSON,                                              -- {type: none|text|photo|number|checkbox|connector}
              grader JSON,                                               -- {kind: key|rubric|self_report|connector, key?, rubric_id?}
              status [open|done|partial|skipped|carried|unknown],
              root_item_id,                                              -- self for new items; carried items keep the root
              ledger_id null, logged_at null, log_source [button|chat|connector|submission] null)

program_submissions(id, user_id, item_id, message_event_id, content_text null, file_ref null,
                    score float null, verdict, feedback JSON,            -- {errors:[{quote, issue, fix}], corrected}
                    grader_kind, model null, graded_at null, status [graded|ungraded|failed])

review_cards(id, user_id, program_id, track_id, front, back, source_item_id,
             fsrs JSON,                                                  -- stability, difficulty, due, reps, lapses
             suspended bool)
```

Notes:
- `plan JSON` is the validated `SessionPlan` (section 6.3) kept for audit and regeneration; items are
  also normalised into `program_items` because the ledger, tools and adaptation address items by id.
- Health and finance logs live only in program tables, never the memory graph, unless the user asks to
  remember something. `forget` and program deletion cover program tables, submissions, files and cards.
- Photo submissions keep the Telegram `file_id` plus a stored copy only while grading is pending (then
  the copy is deleted; the text transcription and grade remain).

## 5. Scheduling

### 5.1 Wakeups (system kinds, never reasoned about)
New kinds, registered with `register_system_wakeup` so they never reach the reasoner
(`timers/system.py:1-40`). All fit the 24-char kind column.

| Kind | Fires | Does |
|---|---|---|
| `system_program_generate` (23) | night before, spread in the window [check-in + 30 min, session time - 60 min] by a stable hash of (user_id, program_id) | builds tomorrow's plan (section 6) |
| `system_program_session` (22) | promised session time | renders and sends the morning slot (5.2) |
| `system_program_checkin` (22) | check-in time | sends the evening slot (5.2) |
| `system_program_close` (20) | just before the next generate | closes today's session (5.5) |

Each payload carries `{program_id, local_date}`. The handler re-reads the program and checks ownership,
status and date before acting (the subject-bound rule from hotfix4, `initiative/subjects.py`). Each handler
books only its own next deterministic occurrence; nothing a model writes can schedule a program wakeup, so
runs cannot chain. Booking uses the existing `schedule_once` style with dedupe key
`prog:{id}:{kind}:{date}`.

### 5.2 Daily slots: one morning message, one evening message
Today the morning check-in (`routines.py:159-203`) and the evening wrap (`rhythm.py:100-162`) each build
their own intent. Programs add a third and fourth sender for the same moments. Rather than special-casing,
introduce a general **daily slot** with contributors:

- A slot (`morning`, `evening`) has a time, a dedupe key (`morning:{date}`, `evening:{date}`, the keys
  already used), and registered contributors. Each contributor returns a `SlotSection`:
  `{title, intent_lines, trusted, appendix_bubbles, promised_time?, buttons?}`.
- Slot time: the earliest promised time among today's contributors if one is within 90 minutes of the
  learned time (`routines.py:224-255`), else the learned or configured time. A promised time more than 90
  minutes away keeps its own message (only programs with `own_time`, or a user whose plan time is far from
  their usual morning).
- The existing morning brief and its `BriefSource`s (`routines.py:49-77`) become one contributor; the
  attention evening wrap becomes one contributor; each program is a contributor to both slots.
- The composer gets the merged intent; code-rendered `appendix_bubbles` (the plan) are appended after the
  composed bubbles; buttons from all sections are stacked (one row per program).
- The ignored-streak count (`routines.py:205-214`, `proactive:morning:%` event ids from
  `executor.py:392`) keeps working because the merged message still uses `morning:{date}`. Program-level
  ignore counting uses `no_signal_streak` (5.5), not message ids.

Whichever wakeup fires first for a slot sends the merged message; the others find the dedupe key taken.

### 5.3 Ping policy integration
- Promised program messages call `notify` with the user-reminder semantics (`PingPolicy.check(...,
  reminder=True)`, `policy/pings.py:87-119`, the `if reminder` branch at :106): deduped, quiet-hours aware,
  never dropped by the daily budget, still counted by `count_today`.
- Per-subject daily slot: `subject_ping_key` (`pings.py:68-76`) hard-codes two slots (`prep` for
  `event_starting`, else `any`). Programs need two different slots per subject per day (plan, check-in).
  Generalise to a declared table `PING_SLOTS = {event_starting: prep, program_plan: plan,
  program_checkin: checkin}` with `any` as default. Each program in a merged slot message reserves
  `subj:prog:{id}:plan` (or `:checkin`) through `PingPolicy.reserve` (`pings.py:177`), so no other path
  (reasoner, LEARN follow-ups) can ping about that program the same day under the same slot.
- Plan and check-in text from programs is trusted (code and Mavis's own generation). Resource excerpts are
  untrusted and never reach the composer (6.4), so the composer's link scrubbing
  (`composer.py:78`) does not fire on plan content and is not weakened.

### 5.4 Quiet hours and time zones
- Times are local to `user.timezone`; all arithmetic uses the existing local-time helpers (DST safe).
- Quiet hours are currently global (`quiet_start 23`, `quiet_end 7`, `config.py:125-126`). A program time
  inside quiet hours is refused at intake with the nearest allowed time. When per-user quiet hours arrive
  with the multi-user work, intake may offer to move them; no program-specific override.
- If a session wakeup is deferred anyway (box down, quiet hours changed), a plan more than 3 hours late is
  sent with the computed note "running late today" and the blocks shown without clock times.

### 5.5 Closing a session
`system_program_close` runs before the next generation:
- Items with a log keep their status; items without one become `unknown` (never `skipped`).
- A session with no log, no reply and no submission is `no_signal`: excluded from adaptation, increments
  `no_signal_streak`. Any logged item resets the streak and counts the day as "showed up".
- Open collectable items are either carried (pack rule `carry_max_days`, default 2) or closed.
- Ledger rows are updated in the same step (10.1); close, not the ledger's 24h timer, decides them.
- Late logs and submissions for the closed day are still accepted until the next session is sent; they
  update the closed session and its completion, and adaptation is re-run for that day.

## 6. Generation pipeline

### 6.1 When and where
- Runs in the worker as a system job at LLM priority `background` (`llm/models.py:147-150`): user-visible
  work off the reply path. Not `best_effort`, which may be skipped; not `interactive`.
- Day 1 is generated right after intake. Later days the night before (5.1), when chat load is lowest.
- Bounded: `run_specialist` (`agents/specialists/base.py:82`) with a `coach` specialist (tier SMART, step
  budget from settings, wall-clock timeout with the existing wrap-up reserve).
- Retries with backoff until the deadline (session time - 45 min). At the deadline, the **deterministic
  fallback** runs: carried items, due review cards and the pack's evergreen items (no links), sized to the
  lite budget. The plan is labelled in data as fallback; the composer is told "short list today" and never
  claims new resources. This keeps G2 and is truthful.
- Caching: per program, the pack syllabus graph and canonical resources are loaded from data; resources
  fetched in earlier runs may be re-used only after a cheap re-check (HEAD or extract) in the current run,
  which records a new `program_resources` row for that run.

### 6.2 Inputs (all computed or stored, never recalled from chat)
- Pack: syllabus or template graph with weights, item kinds, defaults, safety rule ids.
- Program and tracks: level, load minutes, topic cursor, weights, intake answers.
- Last 3 sessions: completion and accuracy per track, missed concepts, carried items.
- Due review cards (count and sample).
- Available minutes tomorrow: `daily_minutes` split by program weight, reduced by calendar busy time in the
  study window (connected calendar via existing tools today; Track 3 `calendar.busy_minutes` later), and by signals the
  pack declares over connector metrics (fitness: last night's sleep below the pack threshold means lite
  default; see 10.4).
- The computed rationale line (section 8), passed through, not regenerated.

### 6.3 Output: `SessionPlan` (strict pydantic, validated by code)
```
SessionPlan {
  items: [{track, kind, title, minutes, difficulty, resource_id?, segment?, collect, grader,
           rubric_id?, answer_key?, review_card_seeds?: [{front, back}]}],
  lite_item_ids: [index],
  carry_item_ids: [id],
  notes_for_composer: str            -- at most one short line, no claims about the past
}
```
Validation (code, before storing):
- `kind`, `collect.type` and `grader.kind` are allowed by the pack; item minutes sum within the day's budget
  (+5%); lite subset within the pack's lite fraction; at least one item per active track unless the pack
  says otherwise.
- Every `resource_id` exists in `program_resources` for **this run** with `http_status` 200; segments only
  when `duration_s` is known and the segment fits inside it.
- Safety post-checks from the pack's rules (section 11) on structured fields (for example a calorie or
  pace target outside the pack's bounds is rejected).
- On a validation failure: one repair turn with the error list, then fallback.

### 6.4 Resource grounding (G3)
- The coach has two program tools instead of raw web tools: `resource_search(query)` and
  `resource_fetch(url)`. `resource_fetch` opens only URLs returned by this run's searches or on the pack's
  canonical domains, the same rule tainted tasks use today (`tools/web.py:295-370`, `record_search_urls` at
  :331, `web_extract` at :367). The difference: provenance is persisted per run in `program_resources`,
  not kept in the bounded in-memory map.
- A fetch records title, canonical URL, status, content hash, kind, duration when the page exposes it, and
  a short excerpt. Excerpts are third-party content: they are wrapped untrusted for the coach and never
  shown to the composer.
- Domain policy is pack data: `canonical_domains` (preferred, e.g. nptel.ac.in, ets.org, gate official
  site; rbi.org.in, investor.sebi.gov.in, amfiindia.com; who.int), `open_web: true|false`, and
  `blocked_domains`. Finance and health packs default to `open_web: false`.
- The plan bubble is rendered by code from `program_items` and `program_resources`: titles and links come
  from the fetched record, never from model text. If no verified resource exists for an item, the bubble
  shows the topic and a search phrase ("search: NPTEL operating systems paging").

### 6.5 Model use summary
| Step | Tier | Priority | Calls |
|---|---|---|---|
| Intake questions and `program_propose` | chat model | interactive | part of normal turns |
| Generation (`coach`) | SMART | background | 1 run per program per day, bounded steps |
| Plan repair | SMART | background | at most 1 |
| Morning and evening composition | FAST (composer) | background | 1 per slot message, not per program |
| Grading (rubric items) | FAST | interactive (user waits); falls back to background with "I'll check and get back" | 1 per submission |
| Red-flag classification | none extra (chat turn field, 11.2) | n/a | 0 |

Per-user caps (settings, multi-user): active programs 3, generation runs per program per day 1 plus
repair, fetches per run 12, graded submissions per day 30.

## 7. Grading and feedback loop

1. **Capture.** `program_submit(item_id, content)` from the chat turn. Code checks the item belongs to
   the user, is in today's session or the previous day's (until the next one is sent), and accepts that collect type.
   Photos arrive as `InboundFile` (`channels/telegram_updates.py:24-31`).
2. **Deterministic first.** Items with an `answer_key` (MCQ, numeric with tolerance, exact token) are
   scored in code. The LLM is used only to explain a wrong answer.
3. **Rubric grading.** A FAST call with the pack rubric, the item prompt and the submission (fenced as the
   user's content) returns
   `{score 0..1, verdict, errors: [{quote, issue, fix}], corrected, concept_tags}`.
   Code validates that every `quote` appears verbatim in the submission and drops errors that do not, so
   the grader cannot invent mistakes. Score aggregation and thresholds are code.
4. **Photos.** If the configured model tier supports images, the grader transcribes then grades and the
   transcription is shown back for disputes. If not, the item is logged as done from the photo, marked
   `ungraded`, and Mavis says plainly that it can only check typed answers for now (open question 3).
5. **Feedback.** The tool result is rendered with the existing result rendering; the chat model writes a
   short reply: what was right, the specific error, the fix, one encouragement. No score number unless the
   pack shows scores.
6. **Effects.** Item status `done` (or `partial` below the pack's pass mark), ledger assignment row closed
   with evidence (10.1), misses become review cards (`concept_tags` plus the corrected version), accuracy
   feeds adaptation.
7. **Failures are truthful.** If grading fails (model error, timeout), the submission is stored `failed`,
   the item is logged done, and the reply says the check did not run and when it will be retried. Never a
   made-up grade.
8. **Integrity.** Pack rule: coach and explain; do not produce answers for live graded coursework or exams
   the user says are being assessed now.

Spaced repetition: FSRS (py-fsrs, MIT) per card; due cards fill the pack's `recall` block (5 to 10 min).
Cards from the user's own misses are prioritised.

## 8. Adaptation rules (deterministic, explainable, pack-tunable)

Per track, at session close, with `c` = share of planned minutes logged done (partial counts half) and
`a` = mean score of graded items (if any). `no_signal` sessions are skipped.

| Condition | Change | Rationale line (code) |
|---|---|---|
| `c >= 0.9` and `a >= 0.85` for 2 sessions | level +1 step, or load +10% (cap +15% per 7 days) | "Two clean days, nudging it up a bit." |
| `0.5 <= c < 0.9` | hold | "Same size today." |
| `c < 0.5` for 2 sessions, or the user said "too much" | load -25%, lite becomes the default plan | "Pulled it back to a lighter list." |
| `a < 0.7` (with `c >= 0.5`) | hold load, difficulty -1 step, add a review item on the missed concept | "Going over yesterday's tricky bit first." |
| user said "harder" | level +1 step from the next session | "Turning it up like you asked." |

- Thresholds, steps and caps are pack data (`adaptation` block) so fitness can use a slower ramp (+10% per
  week) than vocabulary.
- Target success around 85% (Wilson et al. 2019).
- Rationale lines are templates in pack or core data with computed values; the composer receives them as
  facts and may rephrase, not change them.
- Milestones (Day 7, 30, 60, program end) and weekly recaps use computed numbers only: days showed up,
  minutes, accuracy trend.

## 9. Domain packs (data, not code)

### 9.1 Format
One directory per pack under `src/mavis/programs/packs/<pack_id>/`: `pack.yaml` plus optional data files
(syllabus graph, decks, rubrics, answer keys). Loaded and validated against a pydantic `Pack` schema at
startup; an invalid pack fails startup in tests and is skipped (logged) in prod. The engine only reads
fields; it never branches on `pack_id`.

```
id, version, title, match: {description, examples}          -- for intake classification by the chat model
intake: [{field, question_hint, required}]                  -- at most 4 shown
defaults: {session_time, checkin_time, daily_minutes, days_of_week, carry_max_days, lite_fraction}
tracks_template: [{name, topic_graph_ref, weight}]
item_kinds: [{kind, collect: [types], grader: [kinds], minutes: [min, max]}]
rubrics: {rubric_id: {criteria, pass_mark, show_score}}
resources: {canonical_domains, open_web, blocked_domains, canonical: [{title, url, topic_refs}]}
evergreen_items: [...]                                      -- used by the fallback day
adaptation: {up_step, up_cap_week, down_step, hold_band, accuracy_floor}
signals: [{metric, rule, effect}]                           -- 10.4, metric names from connector_metrics
safety: {rules: [rule_id], intake_question, one_time_note}  -- section 11
tone: {celebrate, avoid}                                    -- short hints for the composer
```

### 9.2 First packs
- **education/exam** (GATE CS, GRE): syllabus graph with exam weights; PYQ by topic from official papers;
  vocab decks; sentence-correction and short-answer rubrics; quant answer keys. Rule: generated questions
  are labelled "practice question", never given a year or "previous year" label.
- **education/language** (English writing, vocab): sentence and paragraph rubrics, decks.
- **fitness-habits**: walk, run, strength, mobility, sleep, hydration, steps. Item kinds `habit`, `workout`,
  `log`; collect `checkbox|number|connector`; ramp +10% per week; rest days; RPE self-report.
- **finance-literacy-and-habits**: budget setup, expense logging, savings goal, debt payoff plan
  (avalanche or snowball, computed by code from user numbers), emergency fund, concept lessons (index fund,
  SIP, PPF, NPS, term insurance, tax regimes) from canonical sources, scam spotting.

Adding a domain is a new pack directory plus tests; no engine change.

## 10. Integration points

### 10.1 Commitments ledger (Phase B)
Programs do not keep their own "pending" list. They own plan and progress data; the ledger owns what the
user can be told is pending.

- **Subject keys.** Add `ProgramRef(program_id)` -> `prog:<id>` and `ProgramItemRef(root_item_id)` ->
  `prog-item:<root_item_id>` to the single key function and `PREFIXES` (ledger branch
  `src/mavis/ledger/keys.py`). Carried items keep their root id, so carrying is a merge (same subject and
  type: due moves, no duplicate row).
- **Rows.** One `goal` row per program (`prog:<id>`), live while active or paused. One `deadline` row per
  collectable item (`prog-item:<root>`), due at the session's close time. Non-collectable items (read,
  watch) are not ledger rows; the pending view shows a computed one-line program summary ("GATE Day 5: 2 of
  4 done") from program tables through a pending-view contributor.
- **Single writer by prefix (new, general).** A registry maps subject prefixes to an owner module. For
  owned prefixes, generic writers (LEARN `user_says_done`, `resolve_pending`, reasoner claims) do not
  transition the row; the ledger forwards the claim to the owner's handler, which records the item status
  and then closes the row with evidence. This keeps one source of truth and lets LEARN see program items as
  existing items (no paraphrased duplicates such as "write 5 sentences" as a chat loop).
- **Evidence kinds.** Add `program_logged` (button, chat log, connector) and `program_submitted`. A
  connector-derived log is third-party provenance and is labelled as such ("from Strava").
- **Follow-ups.** Rows with owned prefixes are skipped by the ledger's type-based follow-up scheduler; the
  program check-in is their follow-up. After the check-in is delivered, rows get `FOLLOW_UP_DELIVERED`
  (awaiting_user); no reply leads to `expired`, never `done` (ledger rule: silence closes nothing).
- **Pause and drop.** Pausing closes open assignment rows as `expired` with a note and carries the items;
  dropping the program closes the goal row `dropped` (the user said so).
- Hard dependency: Programs build on the ledger after its readers are switched (ledger build step 5). No
  interim loops-based integration.

### 10.2 Wakeups and the reasoner (hotfix4 rules)
- Program wakeups are system kinds, so the reasoner never sees them (no self-feeding).
- The reasoner sees program goal rows as read-only facts in its ledger view, and never pings about an
  owned subject (the owner registry above). It cannot schedule, adjust or close a program.
- Add `SubjectKind.PROGRAM` to `initiative/subjects.py` (:28-33) so composed program messages carry a
  source record (current day, items, computed state) for the composer's premise check, as other proactive
  pings do since hotfix4.

### 10.3 Chat agent
- Context provider (`agents/context_hooks.py:21-44`): when a program is active, inject a compact,
  labelled live block: "Programs (live): GATE+GRE Day 5, open: #412 5 English sentences (send text), #415
  10 GRE problems (number). Paused: none." Ids let tools address items without fuzzy matching.
- Tools (registered like the existing chat tools, risk `WRITE_SELF` since effects stay with the user, so
  the Track 1 rule means no confirmation card):
  `program_propose(pack_id, title, answers, times)`, `program_log(item_id, status)`,
  `program_submit(item_id, content|file)`, `program_adjust(program_id, change)` (time, pause/resume,
  lighter/harder, add/drop track, days), `program_status(program_id?)`, `program_end(program_id, how)`.
- Buttons: a `prog:` prefix handler in `agents/buttons.py` (:17-21) for check-in buttons; deterministic.
- Persona capability list (`agents/persona.py:47-69`): add one "Working today" line ("daily plans and
  check-ins for a goal like an exam, a fitness habit or money habits") only when `PROGRAMS_ENABLED` is on;
  until then the model must not promise it.

### 10.4 Connectors (Track 3) as signals
Packs declare `signals` as rules over the daily metrics the connectors spec publishes
(`2026-10-08-mavis-connectors-design.md` section 9.3, table `connector_metrics`). Programs read only through
`signals.series(user_id, metric, days)` and `signals.latest(...)`, never raw records. Metric values are
facts for code rules, never instructions, and never reach the composer except as computed rationale lines.
- `calendar.busy_minutes` (and first start, last end) inside the study window reduces tomorrow's minutes;
  a day with less than the lite budget free becomes a lite day: "Packed calendar tomorrow, so a short list."
- `fitness.workouts`, `fitness.active_minutes`, `fitness.steps`: when a day's value meets an item's target,
  the item is auto-logged with `program_logged` evidence (ref `metric:<name>:<local_day>`, third-party
  provenance, shown as "from your fitness app"); the user can correct it.
- `sleep.minutes` below the pack threshold makes the next day lite and swaps hard workouts for mobility.
- `money.spend` and `money.food_orders` pre-fill expense and budget log items; the user confirms or fixes.
- Without connectors, everything works from self-report.

### 10.5 Sandbox (Track 4), later
- Item resource kind `artifact`: generation may request a sandbox job to produce a worksheet PDF, a
  budget spreadsheet or a progress chart; the file is recorded as a `program_resources` row of kind
  `artifact` for that run (same grounding rule: it exists because this run made it) and sent with
  `send_document`.
- Grader kind `exec`: code exercises graded by running tests in the sandbox.
- Not in this track's first release; the schema already allows both.

### 10.6 Multi-user
- Per user: programs, caps, schedules, timezone, quiet hours (when per-user), register, safety pauses.
- Generation spread by a stable per-(user, program) offset across the night window, so 100 users do not
  hit the LLM at 21:00 together; the night window is also when chat is quiet.
- No pack is per user; per-user overrides live in `programs.settings`.
- Invite flow and onboarding may mention Programs once the flag is on; no program is created without the
  user's words.

## 11. Safety

Rules are short, live in a shared safety library referenced by pack `safety.rules`, and are enforced in
code where it matters. Caveats are said once (at intake or the first time a topic arises), never as a
footer, and a decline is one clause plus what Mavis can do instead.

### 11.1 Health and fitness rules (pack prompt text, concise)
- Wellness coaching only: habits, activity, sleep, hydration, general nutrition, and logging symptoms to
  show a doctor.
- Never diagnose, interpret test results as a diagnosis, or start, stop or change medication or doses.
- No extreme targets: calorie, pace, weight-loss rate and fasting bounds are pack data and checked in code
  on structured plan fields; no weight or appearance goals for minors.
- New exercise program plus a heart, blood pressure, diabetes or joint condition, pregnancy, or age 65+:
  suggest a quick check with their doctor, once.
- Persistent or worsening symptoms: "worth getting checked by a doctor", once per topic.
- Eating-disorder signals (purging, extreme restriction): supportive, offer support resources, no calorie
  targets, the program switches to non-food habits until the user changes it.

### 11.2 Red flags (deterministic response, any chat, not only programs)
Red flags: chest pain or pressure, trouble breathing, stroke signs, severe bleeding, suicidal intent or
self-harm, overdose or poisoning, anaphylaxis.
- Detection: the chat turn's structured output gains a `safety` field (none or a category), labelled by
  the same call that writes the reply, so it costs no extra LLM call (Track 1 adds a structured `reaction`
  field the same way). A small, high-precision multilingual phrase list runs before the LLM only as a
  backstop, so the numbers still go out if the model is slow or down. Neither is a reply template.
- Response (code-rendered first bubble, then the model's short warm follow-up): India 112 (all
  emergencies), 108 (ambulance), Tele-MANAS 14416 or 1800-891-4416 (mental health, 24x7). Outside India,
  the local emergency number from a small country table keyed by the user's timezone or stated country.
  Ask whether they are safe and whether someone is with them.
- Side effects: program pings paused for the rest of the local day (`pause_reason: safety`), no reaction,
  no swearing, no jokes, an audit row.

Example first bubble:
> If this is happening now, please call 112 or 108 for an ambulance. If you're thinking about hurting
> yourself, Tele-MANAS is free and 24x7: 14416. Are you safe right now? Is anyone with you?

### 11.3 Finance rules (India, SEBI)
- Education and habits only. Never recommend specific securities, funds, entry or exit points, target
  prices, F&O trades or tips, never project or promise returns, and never give a personalised asset
  allocation as advice. (SEBI (Investment Advisers) Regulations 2013, amended Dec 2024; SEBI finfluencer
  rules separating education from advice.)
- When asked what to buy: one line and move on: "That's adviser territory: a SEBI-registered investment
  adviser (you can check on sebi.gov.in) can tell you what fits. I can explain how the options work."
- Tax: general rules and deadlines only; their own filing goes to a CA.
- Scams: flag guaranteed returns, stock tip groups, UPI collect requests; point to 1930 and
  cybercrime.gov.in.
- Money never moves without approval (existing approval flow).
- Enforcement in code: plan items have no field for a security identifier; a FAST classifier labels
  generated plan text offline (cost is fine at one plan a day) and a plan labelled as advice is repaired or
  falls back.

### 11.4 Education rules
- Practice questions are labelled practice; real PYQs only with a fetched source.
- No answers for assessments the user says are live and graded.
- If sleep or stress comes up, offer the lite plan; do not lecture.

### 11.5 Tone (consumes Track 1)
- Register comes from the Track 1 signal (T1.2). Program additions: the first bubble of a cold proactive
  message is one notch milder than the stored register and has no swearing; no swearing on health, money
  loss, safety or bad-news turns; celebrate wins briefly ("That's the whole list. Day 7, nice.").
- Reactions come from Track 1 (T1.3): completions and submissions are good moments for one; never on
  red-flag turns.
- No em or en dashes in any program copy; the output pass already normalises them.

## 12. Testing

### 12.1 Deterministic (CI)
- Pure functions, table-driven: slot time resolution, quiet-hours refusal, generation window spread,
  session close (unknown vs skipped vs no_signal), carry rules, adaptation table, days-showed-up counter,
  milestone detection, FSRS scheduling.
- Pack schema: every pack loads; a pack with an unknown item kind, grader or rule id fails; no engine code
  references a `pack_id` (a test greps the engine package for pack ids).
- Grounding validator: a plan citing a resource not fetched this run, a non-200 resource, a segment beyond
  the duration, or minutes over budget is rejected; the plan bubble renders only stored URLs.
- Grader validator: errors whose quote is not in the submission are dropped.
- Ledger integration replay: a synthetic week (intake, logs by button and chat, submissions, carried items,
  pause, resume, LEARN re-mentions, `resolve_pending` on a program item) yields one row per subject,
  closures with evidence, no chat duplicates, and the owner registry routing claims.
- Ping policy: two programs plus morning brief yield one morning message; plan and check-in slots both
  allowed the same day; promised messages pass the budget gate and are counted; a reasoner ping on a
  `prog:` subject is refused.
- Safety: the backstop list triggers the fixed first bubble; a `safety` field triggers it; program pings
  pause for the day; finance plan with a ticker or a return promise is repaired or falls back; fitness plan
  outside bounds is rejected.
- Clock matrix (`scripts/test_clock_matrix.sh`) across time zones, DST, midnight and quiet-hour edges.

### 12.2 Chat evals (strict fake LLM)
"Here are my sentences" calls `program_submit` with the right id; "too much" calls `program_adjust`;
"pause GRE till Monday" pauses only that program; "what's pending" shows program items once via the
ledger.

### 12.3 Live evals (real models, live test sink)
Using the live E2E harness and its synthetic test user whose sends go to the log sink (commit dbd2824):
- Generation (10 programs across packs, 3 days each, pinned clock): every link resolves and is in the
  run's fetched set; minutes within budget; no em dashes; rationale matches computed data; fallback path
  exercised by forcing an LLM failure.
- Grading accuracy on a labelled set (50 sentence corrections, 50 quant answers): agreement with labels,
  no invented errors; target set by the owner after a first run.
- Safety red-team (health red flags, medication change asks, crash diets, "which stock", "guaranteed
  returns" groups; English and Hinglish): 100% emergency numbers on red flags, 0 tips, and a judge rubric
  for preachiness (at most one caveat sentence, no repeated disclaimer).
- Tone: register mirrored on replies, milder cold first bubble, no swearing on health or money turns.
- A 7-day simulated run (accelerated clock) end to end: plan, logs, submissions, check-ins, adaptation,
  recap.

## 13. Rollout
- Flag `PROGRAMS_ENABLED` (off by default; off in tests except program tests). Packs individually
  enabled by `PROGRAMS_PACKS`.
- Stage 1: owner only, education packs, 14-day dogfood; measure delivered-on-time rate, days showed up,
  generation failure and fallback rate, grading disputes.
- Stage 2: safety gate (11.2) ships for all chat first, then fitness-habits pack.
- Stage 3: finance pack.
- Stage 4: invited users, with per-user caps; then connector signals as Track 3 metrics land; sandbox
  artifacts after Track 4.
- Deploy with the existing script; the migration is additive and reversible.

## 14. Build order
0. Prerequisites: ledger merged with readers switched (ledger step 5) and renumbered migration; Track 1
   T1.1 (no confirm for self-only tools) and T1.2 register signal.
1. Pack schema, loader and the two education packs; migration; pure core (scheduling math, close,
   adaptation, counters) with table tests.
2. Ledger integration: subject keys, owner registry, evidence kinds, pending-view contributor.
3. Daily slots generalisation (morning and evening contributors, promised time, `PING_SLOTS` table);
   program wakeups; code-rendered plan bubble; deterministic fallback day; check-in buttons.
4. Intake and tools (`program_propose`, `_log`, `_adjust`, `_status`, `_end`), context provider, persona
   line behind the flag.
5. Coach generation with grounded resources and plan validation.
6. Submissions, grading, review cards.
7. Adaptation wiring, weekly recap, milestones; calendar signal.
8. Safety gate for all chat; fitness-habits pack.
9. Finance pack.
10. Track 3 signals (fitness, sleep, money metrics); Track 4 artifacts and `exec` grading.

## 15. Verified code pointers (main 59715fe) and corrections to the research draft
| Pointer | Verified | Note |
|---|---|---|
| `initiative/routines.py:32-38` constants, `:159-203` `_send_morning`, `:205-214` ignored streak, `:216-222` schedule, `:224-255` learned time | yes | clamp 07:00 to 11:00, lead 30 min, `MAX_IGNORED = 3` |
| `attention/rhythm.py:31-50` window and evening sources, `:100-162` `EveningWrap` | yes | evening sources are wrapped untrusted as "From their Google account" (:153-154), so programs cannot just add evening lines: hence daily slots (5.2) |
| `domain/wakeups.py:12-31` kinds, `:47` reserved payload keys | yes | draft's `program_*` kinds become `system_program_*`, routed by `timers/system.py`, so the reasoner never sees them |
| `policy/pings.py:68-76` `subject_ping_key`, `:87-119` `check`, `:106` reminder branch, `:177` `reserve` | yes (draft cited :86-121, :113) | the per-subject slot has only `prep` and `any`: generalised (5.3) |
| `initiative/executor.py:249-311` `notify`, `:392` `proactive_event_id` | yes | notify reserves the subject slot before composing |
| `initiative/composer.py:20-38` rules, `:78` untrusted scrub | yes | web-derived text would have its links scrubbed: links are rendered by code (6.4) |
| `initiative/subjects.py:28-33` `SubjectKind` | yes | add `PROGRAM` |
| `tools/web.py:295-370` URL provenance, `:376-394` tools | yes | provenance is in memory and per task; `web_extract` is not offered to chat: coach gets program tools persisting provenance |
| `agents/context_hooks.py:21-44`, `agents/specialists/base.py:22-35`, `:82` `run_specialist` | yes | |
| `llm/models.py:37-39` tiers, `:147-150` priorities | yes | generation is `background`, not `best_effort` |
| `agents/persona.py:24` mirror line, `:47-69` capability list | yes | register and reactions belong to Track 1, not this spec |
| `memory/profile.py:29-39` profile card (`tone`) | yes | |
| `channels/telegram_updates.py:24-31` inbound photo and document | yes | |
| `agents/buttons.py:17-21` button prefix registry | yes | |
| Draft: "GOAL loop per program", `LoopOrigin.PROGRAM`, migration `0014_programs` | replaced | ledger rows and owner registry (10.1); migration number after the ledger lands |

## 16. Open questions for the owner
1. Message budget: promised program messages pass the 6 a day gate and count toward it (so on program days
   unsolicited pings get fewer slots). Keep that, or exempt them from the count? And is two program messages
   a day (plan and check-in) the ceiling, or do you want optional mid-day block nudges?
2. Health data scope: habits only (activity, sleep, hydration), or also logging symptoms and vitals (blood
   pressure, glucose) for the user's doctor? The second means more sensitive data stored, so retention and
   export rules would follow.
3. Handwritten work: grading photos needs a vision-capable model in the tier (cost and possibly a second
   vendor). Add one for v1, or keep v1 text-only (photos logged as done but not graded)?
