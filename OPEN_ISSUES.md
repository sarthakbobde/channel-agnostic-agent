# Open issues: Release 3, Phase 1 (feedback capture)

Branch `r3/phase-1-feedback-capture`, commit `2e39679`. Written 2026-11-05. Each item says what is open, why it matters and who or what closes it.

Status words: **Open** (nothing done), **Mitigated** (handled in code, still worth confirming), **Blocked** (needs a person or an environment).

## 1. Tests and verification

| ID | Issue | Status | Closed by |
|---|---|---|---|
| T-1 | The full SQLite run after the fixes had not finished when this was written. The first run gave 611 passed, 15 skipped and 4 failed; all four were fixed. | Open | Read the result of the re-run, then update the Phase Report in `PROGRESS.md` |
| T-2 | PostgreSQL and pgvector were not run (Docker Desktop is not running). The plan asks for both databases every phase. | Blocked | Start Docker, then `TEST_DB=postgres python -m pytest tests` |
| T-3 | Slack, Teams and Google Chat were checked only on the payloads they send and receive. No live click was tried: the Slack modal, the Teams card inputs, the Google Chat form inputs. | Blocked | A live test in each workspace, tenant and Chat app |
| T-4 | The Web UI was not opened with a real Chainlit session. The data-layer call and the API are tested. | Open | Run the web chat, rate an answer, check Monitoring > Feedback |
| T-5 | `test_candidates_that_fail_the_checks_are_dropped` (suggestions) failed once, in one particular test order. It passes alone. Not caused by this phase, not investigated. | Open | Find the state that earlier tests leave behind |
| T-6 | Two tests were order dependent and were made independent in this phase (guardrail switches after the multilingual tests; the key command-line test's document folder). Other tests may share state in the same way. | Mitigated | Watch for new order-dependent failures |

## 2. Gaps in what was built

| ID | Issue | Status | Closed by |
|---|---|---|---|
| G-1 | The Web UI collects a rating and a comment, but no reason. Chainlit's own comment box has no reason list. (R3-1 in `DECISIONS.md`) | Open | A follow-up message with reason buttons after a thumbs-down |
| G-2 | When rating is off for a channel or knowledge base, Chainlit's own thumbs stay visible. Nothing is stored; the call is only logged. | Open | Hide the thumbs per answer, if Chainlit allows it |
| G-3 | Comments are masked by the existing masking step, so with personal-data masking switched off a comment is stored as typed. | Open | Decide whether comments must be masked whatever the switch says |
| G-4 | Attachments are accepted as links only. No channel offers a way to attach a link yet; the API field exists. | Open | Decide whether a channel should offer it, or drop the field |
| G-5 | The Google Chat thumbs-down card is returned in the synchronous reply. If a space cannot show form inputs, there is no text-only fallback. | Open | Check in a real space; add a fallback if needed |
| G-6 | `delete_for_conversations` style cleanup exists in two places (memory delete and the retention job). A new place that deletes conversations must also delete feedback. | Mitigated | One shared helper, or a database cascade |
| G-7 | The live app on port 8000 still runs the old code. It needs a restart, and the 0034 migration runs at start. | Blocked | Merge, back up `admin_data/admin.db`, restart |

## 3. Needs a person (not code)

| ID | Issue | Status | Closed by |
|---|---|---|---|
| H-1 | Privacy sign-off for storing comments and attachment links, and for how long ticket evidence may outlive its conversation (D49, D55). | Blocked | Whoever signs off privacy |
| H-2 | Slack: Interactivity must be on and the app reinstalled. | Blocked | A Slack admin |
| H-3 | Teams: confirm the rating card needs no new approval in the tenant. | Blocked | A Teams admin |
| H-4 | Google Chat: card clicks and form inputs on in the Chat app. | Blocked | A Workspace admin |
| H-5 | Confirm the five reason codes (D50) and the full fields of `feedbacks`, `ticket_types` and `ticket_groups`. The plan assumes them (open question 3). | Blocked | The owner of that schema |
| H-6 | Decide D48 to D60: all are **Proposed** in `DECISIONS.md`. | Blocked | The team |
| H-7 | The Hindi texts for the rating controls are Drafts, written by Claude and not reviewed by a native speaker. | Blocked | A reviewer, then approve in the Message catalogue |

## 4. Problems found in the plan (`docs/development-plan-v5-release-3-feedback-ticketing.md`)

These need a decision before Phases 2 to 4. The first two were confirmed against the code; the others come from reading the plan.

| ID | Problem | Where | Suggested fix |
|---|---|---|---|
| P-1 | Phase 1 asks for a unique key on rater and answer, but D49 says a rating after `ticketed` is a new record for the same rater and answer. | Phase 1 task 2, D49 | **Handled** with a plain index and one open record in code (R3-1). Change the plan text |
| P-2 | Retention deletes the conversation and the answer trace, but a feedback item waiting in `human_review` (for example held back by the daily replay cap) needs them. The reviewer would see nothing. | D49, Phase 3 | Keep a masked evidence snapshot on the feedback, or do not delete conversations that have open feedback |
| P-3 | The shared agent runtime is first needed in Phase 2 (the ticketing agent) but is scheduled in Phase 3. Phase 4 is called independent of Phase 3 but also needs it. | D51, Phase 2 task 6, Phase 3 task 2 | Build the runtime in Phase 2, or make the ticketing agent a plain function call |
| P-4 | Tickets already `assigned` to the autonomous queue are stranded when `admin_agent_mode` is turned off or the kill switch is used. Only an Admin reassigning by hand frees them. | A3, Phase 2 task 4 | A rule that releases them to a person when the mode goes off |
| P-5 | The sweeper moves an autonomous `in_progress` ticket to `assigned_to_human` after a timeout. A slow run could then apply a fix and try to finish a ticket that is no longer its to finish. | Phase 4 task 5 | A lease or heartbeat, and a run timeout shorter than the sweeper timeout |
| P-6 | Shadow mode "changes nothing", but the first step of the loop, the claim, changes the ticket's status. It is not said whether shadow mode claims, or what stops it diagnosing the same ticket every cycle. | A4, Phase 4 task 7 | Say whether shadow claims, and record the shadow result so it is not repeated |
| P-7 | There is no post-fix check, so any ticket the agent closes is `completed` whether or not the user's problem is solved. Phase 4 then learns from past tickets' fixes and outcomes. | A4 step 6, D59, Phase 4 | A later signal (a repeat complaint) as an outcome, or do not use outcomes as evidence yet |
| P-8 | A proposed KB setting change goes to `assigned_to_human`, with the assignee set by the assignment rule (default: the department's group). Only an Admin or an Editor of that department may approve it. Nothing says the group's members have that right. | D57, A8 | Route proposals to people who may approve them |
| P-9 | Ticket events are "never updated or deleted" (D56), but retention deletes tickets and their events (Phase 2 task 10). | D56, Phase 2 task 10 | Pick one: archive events, or say they go with the ticket |
| P-10 | `admin_agent_mode` is first built in Phase 4, but Phase 2 already depends on it for autonomous assignment. | Phase 2 task 4 | Create the setting in Phase 2 |
| P-11 | The input of `POST /ticketing/new` has a type `Feedback`, but the seeded ticket types are only the five reasons. | A3, A6 | Say which field holds the source and which holds the type |
| P-12 | The Phase 1 acceptance test (a comment with an email address and an Aadhaar number is stored without them) is true only while masking is on and Aadhaar is in the masked types. | Phase 1 acceptance | State that dependency, or mask comments regardless of the switch (see G-3) |

## 5. Not part of this phase, noted on the way

- The first full run on the latest `main` (before any change here) had 6 failures: five from missing local packages and the language model file (installed and downloaded on this computer), and one test that depended on the order of other tests (fixed in this branch). A clean checkout of `main` on another computer still needs `chainlit`, `fasttext-wheel`, `azure-storage-blob` and `models/lid.176.ftz` to run every test.
- Pull request #10 (README update and a UI label fix) is open and untouched.
