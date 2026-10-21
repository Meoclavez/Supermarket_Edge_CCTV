# Store dashboard: user support plan

Last updated: 2026-10-21. Scope: the store dashboard (`edge_backend/app/static/index.html`), used by
supermarket owners, managers and staff who are not technical.

## Where a first-time user gets stuck (audit)

1. **No store-wide order.** The manual (Part 2) gives a 13-step commissioning order, but the dashboard
   only shows per-camera purpose progress ("Store setup" on Today). Nothing says "draw the plan, then
   place cameras, then calibrate, then phone alerts, then backups", or which of those are done.
2. **Jargon without explanation.** Calibrate, homography-style "Counts into zone", D1/CIF sub-streams,
   tripwire, restricted area, ignore area vs privacy mask, risk score, alert levels, STUN, till key,
   first priority / backup. Most Settings cards start straight with fields.
3. **Setup lives in five places.** Store map (plan, cameras, calibration), camera settings dialog
   (purpose, night watch), Camera setup page (lines and areas), Settings (phones, accounts, online
   access, backups) and Insights. Users do not know which one a step needs.
4. **Help only in the manual.** There is no in-app help, no search, and no support contact.
5. **Raw errors.** New UI paths can surface 401/403/409/423 answers; a few modules show the server's
   text, others a generic "failed".
6. **Empty states** are mostly good (they say "not measured" and why), but a few lists are blank or
   end without a next action.

## What good practice says (sources)

- Help is remembered when it is shown **in context, at the moment of the task**; forced up-front
  tutorials are skipped and forgotten. Keep tours short and skippable; let people reopen them.
  ([NN/g, empty states](https://www.nngroup.com/articles/empty-state-interface-design/))
- An **empty state** should say what the space is for, why it is empty and offer the next action;
  its status message must be accurate. (same source)
- A `?` help icon should be a **button that toggles a disclosure / non-modal popover** (click, Enter,
  Space; Esc closes; `aria-expanded`, `aria-controls`), never hover-only, because touch and keyboard
  users miss hover. ([W3C APG disclosure](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/),
  [GOV.UK-style "get more details"](https://design.homeoffice.gov.uk/design-system/patterns/help-users-to/get-more-details))
- **Error messages**: plain words, what happened, what it means, what to do next; no codes, no
  blame. ([NN/g error-message guidelines](https://www.nngroup.com/videos/error-message-communication-guidelines/),
  [Elastic EUI](https://eui.elastic.co/docs/patterns/error-messages/how-to-write-good-error-messages/),
  [Microsoft](https://learn.microsoft.com/en-us/windows/win32/debug/error-message-guidelines))
- A **setup checklist with progress** (live status, one "Do it" per step, optional steps marked) is
  the common activation pattern for configurable operational products; each step should state why it
  matters.

## Plan (priority order) and status

| # | Item | Status |
|---|---|---|
| 1 | "Getting started" checklist on Today: commissioning steps in the manual's order, live status from the APIs, why + "Do it", progress bar, role-aware, hides steps not in the iKorex plan, dismissible and reopenable | Built (`js/onboarding.js`) |
| 2 | `?` help popovers on key cards and fields, content in one file | Built (`js/help.js`, `js/help_content.js`) |
| 3 | Help centre: header "Help" button, searchable articles, iKorex support contact | Built (`js/help.js`) |
| 4 | First-run welcome for administrators (4 short steps, skippable, reopen from Help) | Built (`js/onboarding.js`) |
| 5 | Plain-language errors helper (`EdgeHelp.friendlyError`) used by the new UI | Built |
| 6 | Empty states with a next action where lists were blank | Built (targeted edits) |
| 7 | Help on the Camera setup page (studio.html) | Deferred |
| 8 | Inline field validation messages across Settings forms | Deferred (per-module refactor) |
| 9 | Undo for destructive Settings actions (remove account, revoke key) | Deferred; they already confirm first |
