# QA false-REWORK recovery continuation

- Owner: ERPcoder
- Scope: continue timed-out `warroom-qa-rework-recovery-20260929`; minimum review-blocker patch only.
- Recovery eligibility now requires the exact latest ERPqa verdict/revision, matching server-recorded ERPqa response delivery, and a structured summary proving the listed execution receipt path does not exist. Client `recovery_code` is only an action selector.
- Approval expiry and target-set binding are revalidated before recovery.
- Evidence reuse dedupes by contract evidence id when present, otherwise by evidence type + URI + SHA256; NULL contract IDs no longer collapse distinct evidence.
- Original Worker PASS/run/evidence and original QA verdict remain immutable; recovery queues exactly one new QA cycle and zero Worker deliveries.
- Production DB/runtime/config/service/deploy/push: not performed.

See `focused-regression.txt` and `static-verification.txt` for exact commands and results.
