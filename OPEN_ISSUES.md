# Open issues: Release 3, Phase 1 (feedback capture)

## 1. Gaps in what was built

| ID | Issue | Status | Closed by |
|---|---|---|---|
| G-1 | The Web UI collected a rating and a comment but no reason. | Closed | The Web UI has its own control (thumbs, then reasons, comment, link), checked live on a copy of the app. Chainlit's own thumbs could not be used: their id is not the message id (R3-1) |
| G-2 | When rating is off for a channel or knowledge base, Chainlit's own thumbs stayed visible and stored nothing. | Closed | Chainlit's thumbs are hidden by `webchat/public/chat.css`; with rating off the Web UI shows no thumbs. The rule follows Chainlit 2.12 (pinned): recheck it when Chainlit is upgraded |
| G-3 | A comment was stored as typed when personal-data masking was switched off. | Closed | Comments are always masked, whatever the question-masking switch says; if masking fails, the rating is refused (R3-1) |
| G-4 | No channel offered a way to add a link. | Closed | An optional link box on the Slack modal, the Teams card, the Google Chat card and the Web UI control. Only http(s) links are kept, at most 5, never downloaded |
| G-5 | The Google Chat thumbs-down card has no fallback in a space that cannot show form inputs. | Mitigated | The rating card now follows the channel's "Buttons on cards" switch, so an admin can turn cards off for such a space. A real space has not been tried (see T-3) |
| G-6 | Cleanup of a conversation's rows existed in two places (memory delete and the retention job). | Closed | One helper, `memory.delete_stored`, over a list of tables; a test fails if a table with a `conversation_id` column is missing from it |
| G-7 | The live app on port 8000 still runs the old code. It needs a restart, and the 0034 migration runs at start. | Blocked | Merge, back up `admin_data/admin.db`, restart |

## 2. Problems found in the plan (`docs/development-plan-v5-release-3-feedback-ticketing.md`)

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

## 3. Tests and verification

| ID | Issue | Status | Closed by |
|---|---|---|---|
| T-1 | The first full SQLite run gave 611 passed, 15 skipped and 4 failed; all four were fixed. The re-run is green: **615 passed, 15 skipped, 2 deselected** (15 min). | Closed | Done; the Phase Report in `PROGRESS.md` still says the re-run was pending and needs this number |
| T-2 | PostgreSQL and pgvector were not run (Docker Desktop is not running). The plan asks for both databases every phase. | Blocked | Start Docker, then `TEST_DB=postgres python -m pytest tests` |
| T-3 | Slack, Teams and Google Chat were checked only on the payloads they send and receive. No live click was tried: the Slack modal, the Teams card inputs, the Google Chat form inputs. | Blocked | A live test in each workspace, tenant and Chat app |
| T-4 | The Web UI was not opened with a real Chainlit session. | Mitigated | Done on a copy of the app with the Azure model: control shown, Chainlit's thumbs hidden, a thumbs-down with reason, comment (email masked) and link stored. Not yet done on the live app or in a second browser |
| T-5 | `test_candidates_that_fail_the_checks_are_dropped` (suggestions) failed in one test order. | Closed | Cause found: `tests/test_platforms.py` left the question-masking switch off for later tests. The fixture now restores every switch it changes |
| T-6 | Three tests were order dependent and were made independent in this phase (guardrail switches after the multilingual tests; the key command-line test's document folder; the platforms fixture's masking switch). Other tests may share state in the same way. | Mitigated | Watch for new order-dependent failures |

## 4. Needs a person (not code)

| ID | Issue | Status | Closed by |
|---|---|---|---|
| H-1 | Privacy sign-off for storing comments and attachment links, and for how long ticket evidence may outlive its conversation (D49, D55). | Blocked | Whoever signs off privacy |
| H-2 | Slack: Interactivity must be on and the app reinstalled. | Blocked | A Slack admin |
| H-3 | Teams: confirm the rating card needs no new approval in the tenant. | Blocked | A Teams admin |
| H-4 | Google Chat: card clicks and form inputs on in the Chat app. | Blocked | A Workspace admin |
| H-5 | Confirm the five reason codes (D50) and the full fields of `feedbacks`, `ticket_types` and `ticket_groups`. The plan assumes them (open question 3). | Blocked | The owner of that schema |
| H-6 | Decide D48 to D60: all are **Proposed** in `DECISIONS.md`. | Blocked | The team |
| H-7 | The Hindi texts for the rating controls are Drafts, written by Claude and not reviewed by a native speaker. | Blocked | A reviewer, then approve in the Message catalogue |

## 5. Not part of this phase, noted on the way

- The first full run on the latest `main` (before any change here) had 6 failures: five from missing local packages and the language model file (installed and downloaded on this computer), and one test that depended on the order of other tests (fixed in this branch). A clean checkout of `main` on another computer still needs `chainlit`, `fasttext-wheel`, `azure-storage-blob` and `models/lid.176.ftz` to run every test.
- Pull request #10 (README update and a UI label fix) is open and untouched.
