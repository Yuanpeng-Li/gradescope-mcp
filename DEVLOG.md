# Gradescope MCP — Development Log

> This file records implementation history, behavior changes, and project-level
> documentation updates. `AGENT.md` should stay aligned with the current state
> summarized here.

---

## Session 10 — 2026-06-05: Warn On Ungraded And Clamped Grade Writes

### What was done

- `apply_grade` and `apply_grade_batch` now surface two grade-write failure
  modes that the locally computed score alone hid:
  - **Ungraded save:** a save with no rubric items checked and no point
    adjustment does not mark the submission graded in Gradescope (the trap
    behind passing `[]` for full credit). The result now warns and points to
    the 0-point "Correct" benchmark item.
  - **Clamped score:** the floor/ceiling capped the result (e.g. adding bonus
    points on a 0-point, ceiling-capped question). The result now reports the
    pre-clamp value and how to fix it.
- `_compute_new_score` gained an opt-in `with_raw=True` that also returns the
  pre-clamp score; the default 2-tuple return is unchanged.
- No behavior change to what is written; reporting only.

### Tests

- Added 4 tests (ungraded warning, clamp warning, `with_raw`, batch ungraded
  flag). Suite: **71 passed**.

---

## Session 9 — 2026-03-18: Full Project Audit And Documentation Refresh

### What was done

1. Read through the full repository structure, server registration layer, core
   tool modules, workflow helpers, tests, and operator-facing docs.
2. Reconciled the documented project state with the actual code in
   `src/gradescope_mcp/server.py`.
3. Updated all primary human-readable project files so they reflect the current
   implementation instead of older snapshots.

### Documentation fixes

- `README.md`
  - Rewritten around the current architecture and real feature set
  - Tool inventory corrected and reorganized by workflow
  - Current counts corrected to **34 tools**, **3 resources**, **7 prompts**
  - Added architecture, constraints, ID semantics, scoring semantics, and
    scanned-assignment notes
  - Added current test count: **30 automated tests**

- `AGENT.md`
  - Rewritten as an accurate maintainer/developer guide
  - Corrected tool counts, test counts, and module responsibilities
  - Added explicit maintenance rule to keep docs in sync with server changes

- `OPERATIONS_LOGS/RECORDS.md`
  - Converted from a minimal placeholder into a clearer mutation-log template
  - Added logging rules for sensitive data handling and rollback expectations

- `.env.example`
  - Clarified that `.env` is loaded automatically by the module entry point
  - Kept the credential surface minimal

- `pyproject.toml`
  - Updated the package description to better match the actual server scope

### Audit findings

- The server currently registers **34** `@mcp.tool()` functions.
- The repository currently contains **30** test functions across 5 test files.
- Earlier docs still mentioned **32** or **33** tools and older test totals.
- The codebase's main operational constraints are:
  - preview-first writes with `confirm_write=True` gating
  - Global Submission ID vs Question Submission ID distinction
  - positive vs negative scoring semantics with always-positive rubric weights
  - `/tmp/gradescope-mcp` as ephemeral cache root for grading artifacts and answer-key material

### Current state

- **34 tools** + **3 resources** + **7 prompts**
- **30 automated tests**
- Core docs synchronized with the implementation as of 2026-03-18

---

## Session 8 — 2026-03-18: JSON Payload Fix, Scoring Defaults, Parallel Grading Tool

### What was done

#### Bug fixes
1. **`apply_grade` / `grade_answer_group` JSON payload** (`grading_ops.py`, `answer_groups.py`):
   - Gradescope's frontend sends `Content-Type: application/json` with `{"rubric_items": {"ID": {"score": "true"}}, "question_submission_evaluation": {...}}`.
   - Old code sent form-encoded `data=` with keys like `rubric_item_ids[ID]=true`, which returned 500.
   - Fix: switched from `data=payload` to `json=payload` with the correct nested structure.

2. **`scoring_type` default was wrong** (`grading_ops.py`):
   - Default was `"positive"` (additive), but Gradescope defaults to `"negative"` (deduction: correct = 0, mistakes = negative weight).
   - Fix: changed fallback in 3 locations from `"positive"` to `"negative"`.

3. **Added scoring direction hints** (`grading_ops.py`):
   - `get_submission_grading_context` now shows: `Rubric items **add** points` (positive) or `Starts at full marks. Rubric items **deduct** points for errors.` (negative).
   - `get_question_rubric` also shows the scoring direction.
   - Prevents agents from using the wrong rubric sign convention.

#### New tool
4. **`list_question_submissions`** (`grading_ops.py`, `server.py`):
   - Scrapes all Question Submission IDs from `/questions/{qid}/submissions`.
   - Supports `filter` param: `"all"`, `"ungraded"`, `"graded"`.
   - Returns JSON with `submission_id`, `student_name`, `graded` status.
   - **Why**: `get_assignment_submissions` returns Global Submission IDs (404 with grading tools). `get_next_ungraded` has race conditions under parallel use. This tool enables the main agent to pre-allocate specific Question Submission IDs to subagents.

#### Skill updates
5. **SKILL.md** (`skills/gradescope-assisted-grading/SKILL.md`):
   - Added parallel grading best practices: one question per subagent, ID pre-allocation via `tool_list_question_submissions`.
   - Added Global ID vs Question ID distinction warning.
   - Added JSON payload debugging hint to safety rules.
   - Previously graded submission skip-by-default policy.
   - `/tmp/gradescope-mcp` file persistence warning for cross-conversation sessions.

#### Docstring corrections
6. **`create_rubric_item` / `tool_create_rubric_item`** — updated weight semantics per scoring type (positive = adds points, negative = deducts points).

### New tests added
- `test_apply_grade_sends_json_payload` — verifies `json=` kwarg with correct nested structure
- `test_positive_scoring_context_shows_add_hint` — verifies positive scoring shows "add points" hint

### Test results
- **20 automated tests** at the time of this session — all passing
- 5 test files

### Files modified
| File | Changes |
|------|---------|
| `tools/grading_ops.py` | JSON payload, scoring_type default, direction hints, `list_question_submissions`, rubric weight convention fix |
| `tools/answer_groups.py` | JSON payload for `grade_answer_group` |
| `server.py` | Import + register `tool_list_question_submissions`, docstring fixes |
| `skills/.../SKILL.md` | Parallel grading policy, ID pre-allocation, batch approval, cross-agent consensus, scoring auto-detection, visual cross-validation, direct grading links, rubric weight guidance, safety rules |
| `tests/test_assignments_and_grading_ops.py` | 2 new tests |

#### Rubric weight convention fix
7. **Rubric weights are always positive** (`grading_ops.py`, `server.py`, SKILL.md):
   - Gradescope stores weights as positive numbers regardless of scoring type.
   - `scoring_type` determines interpretation: positive = earned points, negative = deducted points.
   - A deduction item with `weight=2.0` means "student loses 2 points" — the web UI shows `-2`.
   - Fixed docstrings in `create_rubric_item` and `tool_create_rubric_item` that previously told agents to pass negative values.
   - Updated scoring hints in `get_submission_grading_context` to clarify the positive-weight convention.

#### SKILL design improvements
8. **Batch approval** (SKILL.md):
   - For 50+ submissions, agents collect previews into a summary table (student, score, rubric items, confidence, link).
   - User approves in bulk: "全部通过" / "除了 #3" / "#3 改成 7 分".
   - Batch size: 10–30 per approval round.

9. **Cross-agent consensus** (SKILL.md):
   - Main agent deduplicates rubric gap reports from parallel subagents.
   - N ≥ 2 same-gap reports → one rubric proposal, not N alerts.
   - Subagent return format: `gap_description`, `affected_submission_ids`, `suggested_rubric_change`.

10. **Scoring mode auto-detection** (SKILL.md):
    - Mandatory step before grading: read `scoring_type` from grading context.
    - Stop if rubric weights conflict with stated scoring type.

11. **Visual cross-validation** (SKILL.md):
    - For numerical answers, compare crop vs full-page reading.
    - OCR disagreement → force confidence < 0.6 → flag for human review.

12. **Direct grading links** (SKILL.md):
    - Skipped submissions include clickable Gradescope link: `https://www.gradescope.com/courses/{cid}/questions/{qid}/submissions/{sid}/grade`
    - Post-grading report includes links for all skipped submissions.

### Current state
- **33 tools** + **3 resources** + **7 prompts**
- 20 automated tests (all passing at that point)

---

## Session 7 — 2026-03-18: Bug Fix Sprint (10 fixes across 7 files)

### What was done

Systematic code review identified 12+ potential bugs; 10 were confirmed as real issues and fixed.

#### Critical fixes
1. **`get_next_ungraded` self-loop** (`grading_ops.py`):
   - Gradescope's `next_ungraded` URL points to the *current* submission when it's itself ungraded.
   - Old behavior: returned the same submission the caller was already on.
   - Fix: detects the self-loop, advances via `next_submission`, checks if the next one is ungraded, and returns it. If it's graded, follows *its* `next_ungraded`.

2. **`get_submission_grading_context` self-referencing nav** (`grading_ops.py`):
   - `previous_ungraded`/`next_ungraded` nav entries that point to the current submission are now filtered out to avoid misleading agents.

3. **`prepare_grading_artifact` fabricates "reference answer available"** (`grading_workflow.py`):
   - Rubric-drafted fallback text was passed to `_compute_readiness()` as a real reference answer, inflating the score by +0.2.
   - Fix: only real `explanation` goes into readiness scoring. The rubric draft is labeled "Rubric-Based Fallback" in the artifact.

4. **Readiness score inconsistency** (`grading_workflow.py`):
   - Same root cause as item 3. Both `prepare_grading_artifact` and `assess_submission_readiness` now produce identical scores.

#### High-priority fixes
5. **`get_assignment_outline` missing question IDs** (`grading.py`):
   - Standalone questions (no children) now output `**Question ID:** \`{id}\`` so downstream tools can find them.

6. **Unclear 404 error for wrong submission ID type** (`grading_ops.py`):
   - 404 error now includes a contextual hint: "This often means you are using a Global Submission ID instead of a Question Submission ID."

7. **`apply_grade` / `grade_answer_group` rubric_item_ids coercion** (`grading_ops.py`, `answer_groups.py`):
   - MCP clients sometimes pass a single string `"123"` instead of `["123"]`. Both functions now auto-wrap strings into lists.

#### Medium / low-priority fixes
8. **`get_answer_groups` markdown "Type: (not set)"** (`answer_groups.py`):
   - Falls back to per-group `question_type` when `assisted_grading_type` is None. Added Type column to the table.

9. **`get_assignment_graders` leaking internal IDs** (`submissions.py`):
   - No longer lists the filtered entries' internal IDs/labels. Only reports the count.

10. **`extensions.py` 401 for exam-type assignments** (`extensions.py`):
    - Catches 401 errors and returns a friendly message explaining that some assignment types don't support the extensions API.

11. **Reference answer UX** (`grading_workflow.py`):
    - All 3 "no reference answer" messages now explain this is expected for scanned PDF / handwritten assignments, not an extraction failure.

### Test results
- **18 automated tests** — all passing at the time of this session
- 5 test files

### Files modified
| File | Changes |
|------|---------|
| `tools/grading_ops.py` | Self-loop fix, 404 hint, rubric_item_ids coercion |
| `tools/grading_workflow.py` | Readiness fix, reference answer labeling, UX messages |
| `tools/grading.py` | Standalone question ID output |
| `tools/answer_groups.py` | Type column, rubric_item_ids coercion |
| `tools/submissions.py` | Grader list sanitization |
| `tools/extensions.py` | 401 error handling |
| `tests/test_extensions_and_answer_key.py` | Updated assertions for new messages |

### Current state
- **32 tools** + **3 resources** + **7 prompts**
- 18 automated tests (all passing at that point)

---

## Session 6 — 2026-03-17: Answer Groups, Rubric CRUD, JSON Output

### What was done
1. **Answer Groups** — 3 new tools in `tools/answer_groups.py`:
   - `get_answer_groups(course_id, question_id)` → lists all AI-clustered answer groups with sizes
   - `get_answer_group_detail(course_id, question_id, group_id)` → shows members, crops, graded status
   - `grade_answer_group(course_id, question_id, group_id, ...)` → batch-grades via `save_many_grades`
   - Both markdown and JSON output supported

2. **Rubric CRUD** — 2 new tools in `tools/grading_ops.py`:
   - `update_rubric_item(...)` → modify description/weight (cascades to all submissions)
   - `delete_rubric_item(...)` → remove item (cascades to all submissions)
   - Both have `confirm_write` gates with cascade warnings

3. **JSON Output Mode**:
   - `get_submission_grading_context` now accepts `output_format="json"` for structured data
   - Returns parsed rubric items, navigation, answer_group, progress, pages, crops
   - `get_answer_groups` and `get_answer_group_detail` also support JSON mode

### Key API discoveries
- `/courses/{cid}/questions/{qid}/answer_groups` → full JSON with all groups + submissions
- Group grading uses `save_many_grades` endpoint (not `save_grade`)
- Rubric items support PUT (update) and DELETE on `/rubric_items/{item_id}`
- SubmissionGrader props contain answer group metadata: `answer_group`, `answer_group_size`, `groups_present`

### Test results
| Tool | Test Data | Result |
|------|-----------|--------|
| `get_answer_groups` (markdown) | Q5a (midterm) | OK |
| `get_answer_groups` (JSON) | Q5a | OK |
| `get_submission_grading_context` (JSON) | Q5a sub | OK |
| `update_rubric_item` (dry run) | Q5a | OK |
| `delete_rubric_item` (dry run) | Q5a | OK |
| `grade_answer_group` (dry run) | mocked | OK |

### Current state
- **32 tools** + **3 resources** + **7 prompts**
- 10 automated tests (all passing at that point)

---

## Session 5 — 2026-03-17: Safety Rails, Tests, and Agent Hardening

### What was done
1. Added a two-step confirmation gate for write-capable tools:
   - `tool_upload_submission(..., confirm_write=False)`
   - `tool_set_extension(..., confirm_write=False)`
   - `tool_modify_assignment_dates(..., confirm_write=False)`
   - `tool_rename_assignment(..., confirm_write=False)`
   - `tool_apply_grade(..., confirm_write=False)`
   - `tool_create_rubric_item(..., confirm_write=False)`
2. Fixed upload path validation:
   - uploads now require absolute paths
3. Hardened scanned-page caching:
   - `cache_relevant_pages()` now downloads page images through the authenticated Gradescope session instead of a separate unauthenticated stack
4. Added the first automated test suite
5. Synced project docs at that time

### Why this matters for agents
- Default-deny writes are a better fit for MCP clients, where LLMs may call
  tools speculatively.
- The server now behaves as:
  1. preview intended mutation
  2. execute only with `confirm_write=True`

### Remaining gaps noted at that time
- No structured JSON mode yet for some high-volume read tools
