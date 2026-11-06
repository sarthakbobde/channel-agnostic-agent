# Review of development plan v6.1 (Release 3): issues and suggested fixes

Written 2026-11-06, about `development-plan-v5-release-3-feedback-ticketing (1).md` (draft v6.1). Phase 1 is built on the branch `r3/phase-1-feedback-capture`; Phases 2 to 4 are not started.

## 0. Status

Decision on 2026-11-06: **R-1 to R-7 stay open.** The build goes ahead on plan v6.1 as written, starting with its Phase 1 follow-up list and then Phase 2. Each item remains an accepted risk until it is fixed or closed; nothing here is considered resolved.

## 1. Verdict

Version 6.1 is better than the first plan. It closes every problem found in the first review (P-1 to P-12 and G-1 to G-4), and the review log in the plan's appendix maps each one to a decision or a task. Two additions are good: replay by vote (N runs, K to win) and matching new feedback to open tickets with a link to the earlier closed ticket.

It also brings seven new problems, listed below with a fix for each.

## 2. Issues and suggested fixes

| ID | Severity | Problem | Suggested fix | Changes |
|---|---|---|---|---|
| R-1 | High | **D62: soft delete for ever.** Every conversation delete, including the user's own and the retention job, becomes a soft delete and nothing is ever purged. This conflicts with the "deleted for good" behaviour and wording the app has today, with the privacy sign-off, and with `ticket_retention_days`, which also defaults to "keep for ever". It touches every screen that lists or counts conversations. | Keep a real delete and add a short **hold** for conversations that still have open feedback or an open ticket. Retention skips held conversations and deletes them for good when the feedback or ticket is closed, plus a grace period (for example 30 days). When feedback is captured, copy a **masked snapshot** of the question and answer onto the feedback row, so the review queue and the replay read the snapshot, not the conversation. A user's own delete hides the conversation at once, and the purge happens when the hold ends. One new column (`held_until`) replaces soft-delete flags on every table. | Plan (D62, A6, Phase 1 task 8), code (memory, retention, feedback), privacy sign-off |
| R-2 | Medium | **Matching is the same user only (D55).** Many different users reporting the same bad document make separate tickets, and the admin agent could "fix" the same file several times. | Match on the knowledge base plus at least one shared cited document, whoever raised it. Add the evidence and keep a list of the people involved, without showing who they are to other raisers. Show a count on the ticket ("reported 4 times by 3 people"), which is also a priority signal. The related closed-ticket link follows the same rule, so the agent never repeats a failed fix on that document. | Plan (D55, D64, D67), Phase 2 |
| R-3 | Medium | **Evidence added while the agent is working.** Adding evidence leaves the status unchanged, so the agent can close a ticket it is working on without ever seeing the new evidence. When both an open and a closed match exist, the plan does not say which wins. | If the matching ticket is `in_progress` under the agent, queue the evidence as pending and re-diagnose before completing. Evidence that arrives after completion becomes a new linked ticket. Rule for several matches: prefer an **open** one, then the most recently updated; closed ones only supply the link. | Plan (D55, D63, A4), Phase 2 and 4 |
| R-4 | Medium | **The replay claim can be circular.** For a plain thumbs-down with no comment, the "problem" the judge checks is a summary written by the intent agent itself. | The judge checks a separate, narrow, checkable question taken from the answer and the cited passages (for example "does the answer state a figure that is not in the cited passage?"). If the intent agent cannot name a specific checkable claim, the item goes to human review instead of replay. | Plan (D52, D53, Phase 3) |
| R-5 | Low | **Chat-app alerts hide new work (D58).** The `chat` channel means direct messages in Slack and Teams, with new permissions and a proactive-message setup. | Drop `chat` from this release. Keep `admin_screen` and `email`. Add `chat` later as its own task with its permissions listed. | Plan (D58, Phase 2 task 6) |
| R-6 | Medium | **Phase 2 is now very large.** It holds tickets, the state machine, assignment, matching, evidence, the related link, the ticketing agent, three alert adapters, approvals, reports and retention. | Split it: **2a** (tickets, state machine, assignment, screens, manual handover, retention) and **2b** (matching, the ticketing agent, alerts, reports). Phase 3 needs 2a and matching, so matching stays before Phase 3. | Plan (Part B order) |
| R-7 | Medium | **The Phase 1 text no longer matches what was built.** It says Chainlit's own thumbs are used, but in a live test they could not be tied to an answer, so a custom control replaced them. It drops the reason list that all four channels now show. It cites an older test count (615; now 623). | Update the plan: the Web UI uses a custom control; keep the reason list, or drop it from every channel in one small change if you prefer; the test count is 623. Add to the follow-up list: the hold from R-1, the PostgreSQL run, and the partial unique index for open records. | Plan (Phase 1 task 4, follow-up), code if the reason list goes |

## 3. Suggested order

1. **R-7, R-1 and R-3 first.** They affect what is already built or what the next phase depends on.
2. **R-2, R-4, R-5 and R-6** when Phases 2 and 3 are planned in detail.

## 4. Decisions needed from you

- **R-1:** is a hold with a grace period acceptable for privacy, and how long should the grace period be?
- **R-2:** may different users' feedback on the same document be combined into one ticket?
- **R-7:** keep the reason list on all channels, or drop it everywhere?
- **R-6:** split Phase 2 into 2a and 2b?
