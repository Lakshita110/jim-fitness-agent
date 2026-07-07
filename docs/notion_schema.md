# Notion schema mapping (PLAN.md §12 Q1 + Q3 — resolved)

Discovered from the live workspace on 2026-07-07. IDs are defaulted in
`config.py:Settings` and overridable via env.

## Databases

| Purpose | Database | ID |
|---|---|---|
| Knee+Habit log | `habits db` (under the `habits` page) | `b872f62a28604573980e983be6fd3143` |
| Tasks | `tasks ` (note trailing space in title) | `6843311f33194f40b65ea7e7c0f47436` |
| Proposals | `training proposals` (created 2026-07-07, under `habits`) | `67d2cfc3c75442c4b373736ad38b1cda` |
| Check-in | `training check-in` (created 2026-07-07, under `habits`) | `b789621918c74bd58568eec9218aeb4c` |

`My Tasks` also exists in the workspace but is an empty shell (no data source)
— ignore it.

## habits db properties

| Property | Type | Mapping |
|---|---|---|
| `name` | title | ignored (e.g. "@May 27, 2026 - habit journal") |
| `date` | date | `NotionDay.day` query key |
| `pain level` | number (0–10) | `pain_level`; **often blank** — see below |
| `knee pain` | multi-select | mixes severity (`none/mild/moderate/severe`) with locations (`left/right/ankles/hips/quads/shins`); locations → `pain_location`, severity → fallback `pain_level` (none=0, mild=2, moderate=5, severe=8) |
| `pain location` | select (`none/both/right/left`) | fallback for `pain_location` when `knee pain` has no locations |
| `pain notes` | rich text | `pain_notes` |
| `physical therapy` | checkbox | `pt_done` (excluded from `habits`) |
| `cardio`, `reading`, `strength training`, `vitamins`, `dental care` | checkbox | `habits` dict (any new checkbox is picked up automatically) |
| `day score` | formula (number) | `day_score` |

## tasks properties

Title is `task`; dates are `do date` and `due date`; `status` is a status
property (`Not started` / `In progress` / `Done`). "Tomorrow's tasks" =
(do date = tomorrow OR due date = tomorrow) AND status ≠ Done.

## training proposals properties

`name` (title), `date` (date), `kind` (select: strength/conditioning/
mobility/rest), `status` (select: proposed/approved/rejected/pushed),
`research used` (checkbox). The nightly agent writes rows as `proposed`;
morning approval flips them to `approved`, and auto-push (post-M5) will mark
`pushed`. Plan steps + rationale go in the page body.

## training check-in properties

`name` (title), `date` (target training day), `note` (rich text — free-form
preferences/pain), `focus` (select: no preference/upper/lower/full body/
conditioning/pt only/rest), `location` (select: gym/home), `minutes` (number),
`energy` (select: low/normal/high). The nightly run reads the row dated
*tomorrow*; all fields optional. Home vs gym `location` selects the PT variant.
The morning job re-reads today's row and re-plans if its `last_edited_time` is
newer than last night's proposal (morning check-ins are honored, not dropped).

## Remaining runtime setup

The agent hits the official Notion API with `NOTION_TOKEN`. Create an internal
integration at notion.so/my-integrations, then share **all four databases**
(knee log, tasks, proposals, check-in) with it (⋯ menu → Connections). Without
the share, queries 404.
