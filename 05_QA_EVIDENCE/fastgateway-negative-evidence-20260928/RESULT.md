# FastGateway Negative Evidence Fix

Date: 2026-09-28

## Incident

War Room Simple Mode test Task 406c9c56-d0c0-46e8-a702-008fa94a52a4 returned SIMPLE_UI_TEST_PASS from ERPmanager, but FastGateway marked the run FAIL.

Observed delivery error:

    EVIDENCE_VALIDATION_FAILED:EVIDENCE_UNVERIFIED

Root cause: the evidence text said merge/push/deploy were not executed. The validator matched the word deploy as a positive deploy claim before considering that the sentence was a denial.

## Fix

- Denial patterns are evaluated before positive action claims for each action.
- Korean/English mixed deploy denial forms such as "deploy ... 실행하지 않았다" are recognized.
- If deploy was actually observed while evidence denies it, the validator still returns EVIDENCE_CONTRADICTION.
- Positive unobserved deploy claims remain fail-closed as EVIDENCE_UNVERIFIED.

## Focused QA

- 5 focused evidence tests passed.
- Includes exact Simple Mode denial wording regression.
- Includes contradiction case with an observed wrangler deploy tool call.

## Full QA

FastGateway:
- 221 passed
- 62 subtests passed

War Room:
- 221 passed
- 7 existing deprecation warnings
- 5 subtests passed

Initial combined test run in a clean git worktree produced War Room import failures because static/vocal-coach is an existing untracked runtime directory. After recreating that test-only directory, both suites passed independently.

## Production behavior

The failed Task history is intentionally preserved. After deployment it can be retried from Simple Mode with:

    재승인 준비 -> 승인·실행

No historical result or audit row is rewritten.
