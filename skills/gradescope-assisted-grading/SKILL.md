---
name: gradescope-assisted-grading
description: Interactive, human-approved grading workflow for Gradescope assignments using the Gradescope MCP server. Use when helping an instructor or TA grade by first asking clarifying questions, establishing the grading contract, then previewing any rubric or grade mutation before execution.
---

# Gradescope Assisted Grading

Use this skill when grading through the Gradescope MCP server.

The agent is not a silent auto-grader. Its job is to:
- interview the user first
- establish a grading contract
- gather the right Gradescope context
- propose grades or rubric changes
- wait for explicit approval before any write

## Core Behavior

- Match the user's language.
- Start with questions unless the user has already provided enough detail to grade safely.
- Ask only the minimum questions needed to unblock the next decision, usually 2-5 at a time.
- After each intake round, summarize the current grading contract and call out anything still missing.
- Preview first. Every write-capable tool must be called once with `confirm_write=False` before any `confirm_write=True`.
- Approval before execution. Never post grades or mutate the rubric without explicit approval of that exact action.
- Read before grading. Never grade without reading the student's actual work or a clearly representative answer-group sample.
- Skip ambiguity. If the grade is not precise and defensible, stop and ask or flag for human review.
- Preserve user authority. User-provided answer keys, grading notes, and rubric guidance override inferred answers.
- Prefer structured output. When a tool supports `output_format`, prefer `output_format="json"` for planning.
- Default to preserving existing grades. If a submission already appears graded, skip it unless the user explicitly asks for audit, regrade, or overwrite behavior.
- Default to no submission-specific comment. Only write `comment` when the user wants comments, a one-off `point_adjustment` needs explanation, or a review handoff note is necessary.
- Do not confuse "leave unchanged" with "clear". In `tool_apply_grade`, `rubric_item_ids=None` means keep current rubric state, while `rubric_item_ids=[]` means clear all rubric items.
- In `tool_grade_answer_group`, always pass explicit `rubric_item_ids`. Never rely on inherited rubric state.
- Unless the question clearly indicates otherwise, think in deduction mode first: start from full credit and identify mistakes. Then verify the actual `scoring_type` before any write.
- Rubric weights are always positive numbers. Gradescope's `scoring_type` determines whether they add or deduct.

## Ask-First Intake

Before doing tool-heavy work, determine what the user is trying to do. The user should feel guided, not interrogated.

### First decide the mode

Classify the request into one of these modes:
- discovery / setup
- rubric review or rubric drafting
- grade one sample submission
- grade remaining ungraded submissions
- answer-group triage / batch grading
- review regrade requests
- audit or regrade previously graded work

### Questions to ask first

If the user has not already provided enough context, ask focused questions like:

1. Which `course_id`, `assignment_id`, and `question_id` should we work on?
   A Gradescope URL is acceptable if the IDs are not handy.
2. What do you want to do right now?
   Examples: discover the assignment, build the rubric, grade one example, batch-grade, finish all remaining, review regrades.
3. Do you already have reference answers, grading notes, or a grading policy I should follow?
4. Are there lateness, grace-period, resubmission, or multiple-attempt rules I should respect beyond Gradescope defaults?
5. Should I grade only the most recent attempt whenever there are multiple submissions, unless you say otherwise?
6. Should ambiguous or illegible cases be skipped, surfaced to you for a decision, or graded conservatively?
7. Do you want comments written into Gradescope, or rubric-only grading by default?
8. For larger grading runs, do you want per-submission approval or batch approval in groups of 10-30 previews?

If the assignment type matters and is still unclear, ask:
- Is this online homework, scanned PDF, handwritten exam, code assignment, or mixed?

If the scoring policy is still unclear, ask:
- Do you expect deduction-from-full-credit grading or earned-points grading here?

Do not dump all questions if the user already answered half of them. Ask only what is missing.

### Policy-level questions are required

When grading reveals a policy choice that may recur, stop and ask the user directly instead of silently inventing a rule.

Typical policy questions:
- Should missing units be a reusable deduction item or a one-off exception?
- Does correct method with arithmetic error get partial credit? How much?
- Should notation mistakes lose points if the final answer is still mathematically correct?
- Is this rubric gap reusable across many submissions, or only for this one student?

### Policy drift stopping rule

If the observed submissions show that the original grading contract is no longer stable, stop and ask the user to re-negotiate the contract or update the rubric.

Treat any of these as policy drift:
- more than roughly 10% of a preview batch needs one-off `point_adjustment`
- the same rubric gap appears in multiple submissions
- the lateness or resubmission policy changes the grade outcome repeatedly
- the agent keeps asking the same policy question for new submissions

### What a good follow-up looks like

Prefer short, operational questions:
- "For Q3, should method-only work with no final answer get partial credit?"
- "Do you want me to skip all borderline handwriting cases, or bring them to you one by one?"
- "I found a recurring case not covered by the rubric. Should I propose a new rubric item before continuing?"

Avoid vague prompts like:
- "Any other thoughts?"
- "How would you like me to proceed?" when the real choices are already clear

## Grading Contract

Before grading or mutating the rubric, summarize the current contract back to the user. Include:
- scope: `course_id`, `assignment_id`, `question_id`
- task: what will be graded or reviewed
- reference source: user notes, instructor answer key, rubric-only fallback, or agent-drafted basis
- scoring assumption: positive or negative, and whether it still needs tool verification
- lateness / grace-period / multiple-attempt policy
- comment policy
- ambiguity policy
- approval mode
- whether previously graded submissions will be skipped or revisited
- whether the rubric is considered locked for batch or parallel grading

If any item materially affects grading decisions, ask the user to confirm or correct it before proceeding.

If the user says "you decide", propose a concrete default contract and ask for confirmation.

## Discovery And Grounding

If the user does not provide IDs:
- Call `tool_list_courses`
- Call `tool_get_assignments(course_id)`
- Call `tool_get_assignment_outline(course_id, assignment_id)`
- Call `tool_get_grading_progress(course_id, assignment_id)`

If the user gives a question URL or a bare `question_id` but no reliable `assignment_id`:
- Start with `tool_prepare_grading_artifact(course_id, assignment_id="", question_id)` or `tool_assess_submission_readiness(...)`
- Those helpers may auto-resolve the owning assignment; capture and reuse the resolved `assignment_id`
- Only fall back to manual assignment scanning if auto-resolution fails

Only record leaf questions with non-zero weight.

Skip fully graded questions unless the user explicitly asks for regrading, audit work, or rubric-backfill work.

## Build The Grading Basis

Call `tool_prepare_answer_key(course_id, assignment_id)` once per assignment and read `/tmp/gradescope-mcp/gradescope-answerkey-{assignment_id}.md`.

Treat that file as a grading-basis cache, not automatically as a true answer key.

Interpret it carefully:
- If the user provides reference answers, save them to `/tmp/gradescope-mcp/gradescope-user-reference-{assignment_id}.md` and treat them as highest priority.
- If structured instructor answers exist, use them.
- If structured answers are missing for scanned PDF or handwritten assignments, treat that as normal.
- Do not hallucinate a true answer key from placeholder text.
- If needed, draft a fallback grading basis from the prompt, rubric, and domain knowledge, but treat it as internal guidance only.

For each question, call `tool_prepare_grading_artifact(course_id, assignment_id, question_id)` and read `/tmp/gradescope-mcp/gradescope-grading-{assignment_id}-{question_id}.md`.

Use the artifact to gather:
- prompt text or page-reading guidance
- rubric item IDs and descriptions
- readiness notes
- crop regions and relevant page URLs
- whether the question uses positive or negative scoring

### Reference priority

Use this priority order:
1. User-provided reference answers or grading notes
2. Instructor-provided structured reference answers from Gradescope
3. Agent-drafted grading basis from prompt + rubric + subject knowledge

If the user-provided answer conflicts with the rubric, stop and ask whether the rubric should be updated before grading continues.

### Scoring mode is mandatory

Before grading any question, read `scoring_type` from `tool_get_submission_grading_context` or `tool_prepare_grading_artifact`.

Interpret it this way:
- `positive`: selected rubric items add earned points
- `negative`: selected rubric items are deductions from full credit

Never begin grading a question without confirming its scoring mode. Using the wrong convention will systematically misgrade the entire question.

If the scoring mode seen in tool output conflicts with the user's expectation, stop and ask which interpretation is correct before any write.

If the user's lateness or resubmission policy conflicts with Gradescope's default score state, stop and ask before using rubric changes or `point_adjustment` to compensate.

## Rubric Review Loop

If the rubric is incomplete, unclear, or inconsistent with the user's grading policy:
- Draft the rubric change in chat first
- Explain why it is needed
- State whether the issue is reusable or one-off
- Ask the user to approve the rubric mutation before calling any rubric write tool

When asking the user, make the policy question concrete:
- "Should this become a reusable deduction item for all submissions on Q2?"
- "Is this worth a new rubric item, or do you want a one-off point adjustment only for this student?"

Rubric mutation rules:
- Preview with `confirm_write=False` first
- Only after approval call `tool_create_rubric_item`, `tool_update_rubric_item`, or `tool_delete_rubric_item` with `confirm_write=True`
- Never pass negative weights to rubric creation or update tools
- Remind the user that rubric edits and deletions can retroactively affect previously graded work
- **Maximum 9 rubric items per assignment.** If an assignment has more than 9 sub-parts, consolidate related sub-questions into grouped items and adjust point values accordingly.
- **Always create rubric items in ascending order of weight** (smallest deduction first, largest deduction last). The existing "Correct" item (weight 0) stays at the top.
- **Never use em-dashes (the `—` character) in any output, rubric descriptions, file edits, or responses.** Use a regular hyphen, comma, colon, or parentheses instead. This applies everywhere, not just rubric work.

After any rubric mutation:
- Re-fetch the rubric with `tool_get_question_rubric`
- Show the updated rubric back to the user
- Confirm that the grading contract is still correct before continuing

## Choose A Grading Strategy

First check for answer groups:
- Call `tool_get_answer_groups(course_id, question_id, output_format="json")`

If the response shows `assisted_grading_type="not_grouped"` or `num_groups=0`:
- Do not keep probing answer groups
- Switch to manual sampling with `tool_list_question_submissions(course_id, question_id, filter="ungraded")`

Prefer batch grading when:
- answer groups exist and are readable
- the question is objective enough for group-level judgment
- representative samples are clearly homogeneous

Prefer individual grading when:
- no answer groups are available
- the question is subjective, proof-based, explanation-heavy, or high-risk
- handwritten answers require per-submission reading
- representative samples inside a group are inconsistent

### Mixed-format priority rule

If a question has a mix of typed and handwritten answers, use this hierarchy:
1. typed answers with usable structured text or clearly readable typed crops
2. scanned or handwritten answers that need page-reading tools

Operational rules:
- Clear the typed queue first so rubric gaps and policy drift show up on easier submissions before spending time on handwriting.
- If answer groups exist, grade clearly typed and homogeneous groups before any handwritten-only pass.
- If working submission-by-submission, partition `tool_list_question_submissions(..., filter="ungraded")` into a typed-first queue and a handwritten queue after the first context read.
- Treat a submission as typed-first when `tool_get_submission_grading_context` provides enough readable answer content that `tool_smart_read_submission(...)` is not needed.
- Treat a submission as handwritten when grading depends on crop, page, or handwriting interpretation.
- Do not interleave the two queues unless the user asks for it or a blocking policy issue requires an early handwritten example.

If the best strategy is not obvious, ask the user:
- "This question has usable answer groups. Do you want speed via batch grading, or a slower submission-by-submission pass?"

Do not start batch approval or parallel grading until:
- the grading contract has been summarized and confirmed by the user
- the current rubric has been reviewed and is considered locked for this run

## Batch Grading

For each candidate group:
- Call `tool_get_answer_group_detail(course_id, question_id, group_id, output_format="json")`
- Read the representative crop or inferred answer
- Compare it against the grading basis and rubric
- Decide explicit `rubric_item_ids`
- Default `comment=None` and `point_adjustment=None`

Inferred-member safety:
- Inspect `inferred_count` and `inferred_submissions`
- `save_many_grades` may apply the grade to both confirmed and inferred members
- If inferred members exist, surface that risk to the user in the preview
- If inferred answers are not clearly equivalent, do not batch grade that group

Preview first:
- Call `tool_grade_answer_group(..., confirm_write=False)`

Then ask a direct approval question, including:
- group ID and title
- confirmed count and inferred count
- rubric item IDs you intend to apply
- expected score impact
- one short justification
- confidence

Example approval question:
- "Apply this rule to answer group `17`? Confirmed: 12, inferred: 3, rubric items: `[101, 104]`, no comment, no point adjustment."

Only after explicit approval:
- Call `tool_grade_answer_group(..., confirm_write=True)`

If the group is too ambiguous or the batch write looks risky:
- fall back to individual grading

## Individual Grading

Single-agent navigation:
- Use `tool_get_next_ungraded(course_id, question_id, output_format="json")`
- If the question is mixed-format, defer handwritten-only submissions into a manual queue and continue until the typed-first queue is exhausted

Parallel or subagent grading:
- Do not use `tool_get_next_ungraded`
- Pre-allocate IDs with `tool_list_question_submissions(course_id, question_id, filter="ungraded")`
- For mixed-format questions, split those IDs into typed-first and handwritten queues before assigning work

For each submission, in typed-first order when formats are mixed:
- Read grading context with `tool_get_submission_grading_context(..., output_format="json")`
- If it is already graded, skip by default unless the grading contract says otherwise
- If legibility or completeness is uncertain, call `tool_assess_submission_readiness(...)`
- For scanned work, call `tool_smart_read_submission(...)` and follow the tiered read order
- If local visual inspection is needed, call `tool_cache_relevant_pages(...)` and inspect the cached files in `/tmp/gradescope-mcp`

Queue discipline for mixed-format questions:
- Finish the typed-first queue unless a policy conflict forces escalation.
- Record deferred handwritten submission IDs so they can be revisited in a second pass.
- After the typed-first queue is stable, continue to the handwritten queue with the same rubric and policy contract.
- If the handwritten queue reveals a new recurring rubric gap, stop and bring that policy change to the user before continuing.

### Readiness-first rule

- If readiness is low, do not spend a large amount of effort on speculative grading
- Distinguish `missing structured context` from `ungradable`
- Scanned PDF questions may still be gradable from crop/page evidence plus rubric
- Skip only when the handwriting, crop, or page evidence is still insufficient after bounded reading

### Tiered reading order

1. crop region only
2. full page if the crop is truncated or unclear
3. adjacent pages if the reasoning spills across pages

### Visual cross-check for scanned work

- For numerical answers, compare crop-region reading against full-page reading
- If small visual features like minus signs, decimals, or exponents are ambiguous, force low confidence and flag for human review
- If the crop cuts through handwriting, read the full page before deciding

### Stop and handoff rule

If the submission is still not confidently gradable after Tier 3 reading, stop and hand it off instead of continuing speculative analysis.

### When to ask the user mid-grading

Stop and ask the user when:
- a borderline case depends on policy, not just reading
- the rubric cannot express a recurring case
- a grade is defensible only if one of two plausible policies is chosen
- the student's work is legible but the partial-credit philosophy is unclear

Do not silently convert a policy disagreement into a `point_adjustment`.

### Propose, then ask for approval

If the answer is gradable:
- decide `rubric_item_ids`
- leave `comment=None` by default
- use `point_adjustment` only for narrow one-off cases
- set an honest confidence score

Preview the grade:
- Call `tool_apply_grade(..., confirm_write=False)`

Then show the user:
- student name and submission ID
- selected rubric item IDs
- expected score impact
- comment, if any
- confidence
- one short rationale
- direct grading link:
  `https://www.gradescope.com/courses/{course_id}/questions/{question_id}/submissions/{submission_id}/grade`

Ask an approval question instead of only dumping the preview.

Example:
- "Apply this grade to submission `12345` for Alice: deductions `[88, 92]`, expected score `8/10`, no comment, confidence `0.89`?"

Only after explicit approval:
- Call `tool_apply_grade(..., confirm_write=True)`

## Batch Approval For High-Volume Grading

For large classes, per-submission approval may be too slow. In that case:

1. Collect multiple previews with `confirm_write=False`
2. Present a compact table with student, submission ID, rubric items, expected score, confidence, and link
3. Ask the user for a bounded approval round of 10-30 submissions
4. Execute only the approved rows with `confirm_write=True`

Suggested table shape:

```markdown
| # | Student | Submission ID | Expected Score | Rubric Items | Confidence | Link |
|---|---------|---------------|----------------|--------------|------------|------|
| 1 | Alice   | 12345         | 8/10           | [88, 92]     | 0.89       | [grade](...) |
```

Accept natural-language approvals in the user's language, for example:
- "全部通过"
- "approve all"
- "除了 #3，其余通过"
- "#3 改成 7 分"

After each executed write in batch mode:
- Re-fetch `tool_get_submission_grading_context(..., output_format="json")`
- Verify that the live result matches the preview
- Stop the batch if there is any mismatch

Offer batch approval proactively when:
- more than 20 submissions remain
- many submissions share the same pattern
- the user explicitly asks for speed

## Point Adjustments

Use `point_adjustment` only when:
- the current submission has a defensible edge case the rubric does not express well
- the exception is local to this submission
- the rubric is otherwise sound

Do not use `point_adjustment` when:
- the same gap is likely to recur
- the issue reveals a broken or incomplete rubric
- you are compensating for uncertainty instead of making a precise grading judgment

Decision rule:
1. Try grading with `rubric_item_ids` alone
2. If that is insufficient, ask whether the gap is reusable
3. If reusable, pause and escalate to rubric review
4. If clearly one-off, use `point_adjustment` with a specific explanation

## Parallel Safety

- `tool_get_assignment_submissions` returns global submission IDs, not question submission IDs
- grading tools require question submission IDs
- before any delegation, the main agent must have a confirmed grading contract and a locked rubric
- for parallel grading, use `tool_list_question_submissions`, partition the IDs, and keep batches non-overlapping
- do not let subagents call `tool_get_next_ungraded`
- the main agent owns the grading contract, user approval flow, and rubric policy

If multiple subagents report the same rubric gap, deduplicate those reports before bringing them to the user.

## Post-Grading

After each question or grading pass:
- Call `tool_get_grading_progress(course_id, assignment_id)`

At the end:
- Call `tool_get_assignment_statistics(course_id, assignment_id)`
- Report graded counts, skipped submissions, and any low-scoring questions that may indicate rubric issues
- For every skipped submission, include a direct grading link

## Safety Rules

- Never post grades without explicit user approval.
- Never mutate the rubric without explicit user approval.
- Never guess on illegible or ambiguous work.
- Always state uncertainty honestly.
- Treat missing structured reference answers on scanned PDF assignments as normal, not as an extraction failure.
- If the user supplies reference answers, preserve them in `/tmp/gradescope-mcp` and use them consistently during the run.
- At the start of a new conversation, do not assume prior `/tmp/gradescope-mcp` reference files still exist.
- Before grading, verify whether the question is positive-scoring or negative-scoring.
- For write previews, show the exact rubric item IDs, point adjustment, and comment that would be sent.
- Do not use submission-specific adjustments as a substitute for fixing a rubric that affects many students.

## Minimal Tool Order

Use this default order unless the user directs otherwise:

1. Ask intake questions and summarize the grading contract
2. `tool_get_assignment_outline`
3. `tool_get_grading_progress`
4. `tool_prepare_answer_key`
5. `tool_prepare_grading_artifact`
6. `tool_get_question_rubric`
7. Optional rubric review and user approval loop
8. `tool_get_answer_groups` to choose batch vs individual grading
9. `tool_list_question_submissions(filter="ungraded")` for ID planning or parallel work
10. Batch path:
    `tool_get_answer_group_detail` -> preview -> approval question -> execute
11. Individual path:
    `tool_get_submission_grading_context` -> split typed-first vs handwritten queue when needed -> `tool_assess_submission_readiness` if needed -> `tool_smart_read_submission` if needed -> `tool_cache_relevant_pages` if needed -> preview -> approval question or batch approval table -> execute
12. `tool_get_assignment_statistics`

## Failure Handling

- `AuthError`: stop and report immediately
- `404` on a submission: re-orient with `tool_get_next_ungraded`; the caller may have used a global submission ID
- If live Gradescope state conflicts with a cached summary, trust the live readback
- Repeated low-confidence or skipped cases on the same question: pause and ask the user how to proceed
- If more than roughly 30% of a question's submissions are being skipped, stop auto-grading that question and escalate
- If a preview shows an unintended empty rubric state, stop. That usually means `rubric_item_ids=[]` was passed when `None` was intended
