# Product backlog - n8n Extract Sync

**Project:** n8n Extract Sync  
**Goal:** TODO - one sentence on the problem this solves.

---

## Legend

| Symbol | Meaning                           |
| ------ | --------------------------------- |
| **P0** | Must-have - blocks launch         |
| **P1** | Important - needed soon           |
| **P2** | Nice-to-have - future enhancement |
| **P3** | Low priority                      |
| **S**  | Small - roughly <= 2 hours        |
| **M**  | Medium - roughly 2-6 hours        |
| **L**  | Large - roughly 6+ hours          |

---

## Phase 1: Foundation

| ID  | Task | Priority | Effort | Status | Notes |
| --- | ---- | -------- | ------ | ------ | ----- |
| 1.1 | TODO | P0 | S | Not started | |

---

## Phase 2: Sync + Conflict Handling

| ID  | Task | Priority | Effort | Status | Notes |
| --- | ---- | -------- | ------ | ------ | ----- |
| 2.1 | Add conflict detection and resolution handling for workflows touched by `status` or `sync-two-way` | P1 | M | Not started | Surface overlapping local/remote changes clearly and require an explicit merge or override decision before applying updates. |
| 2.2 | Add an optional parameter to hide `clean` items in sync script output for `status`, `push`, `sync-two-way`, `backup`, and related commands | P2 | S | Not started | Reduce noise when users only want to review workflows that need attention. |

---

## Icebox (unscheduled ideas)

- TODO: capture ideas that are not yet prioritized.
