# Decisions

Each entry uses exactly Date, Decision, Reason, Supersedes, and Status.

## Decision 1

- Date: 2026-09-04
- Decision: Retire Main-centered manual orchestration.
- Reason: Manual worker selection and lifecycle judgment create unverifiable authority and scope drift.
- Supersedes: Main manually selecting workers and orchestrating execution.
- Status: VERIFIED as the governing direction; current dirty-tree acceptance is UNVERIFIED.

## Decision 2

- Date: 2026-09-03
- Decision: Use LOCAL for production work with CLOUD expert escalation only when explicitly approved and policy-bound.
- Reason: LOCAL-FIRST is the default and CLOUD must remain minimal, visible, and bounded.
- Supersedes: Direct or implicit CLOUD selection and provider substitution.
- Status: VERIFIED as policy; complete current LIVE acceptance is UNVERIFIED.

## Decision 3

- Date: 2026-09-04
- Decision: Prohibit War Room from being an execution authority.
- Reason: War Room must control approval, readiness, observation, projection, and stop without creating competing run truth.
- Supersedes: War Room or adapter-led execution authority.
- Status: VERIFIED for the reported Phase 1 scope.

## Decision 4

- Date: 2026-09-04
- Decision: Do not use a separate Command Center service on port 8790.
- Reason: A separate service would create an additional authority and deployment boundary.
- Supersedes: Standalone 8790 Command Center service/port proposal.
- Status: VERIFIED as the current architecture decision.

## Decision 5

- Date: 2026-09-05
- Decision: Prohibit wholesale merging of the reference `core-engine-phase2` architecture.
- Reason: Only explicitly evidenced, gap-checked contracts may be adopted; wholesale merging causes architecture and scope drift.
- Supersedes: Reference-core wholesale merge approach.
- Status: VERIFIED as a change-control decision.

## Decision 6

- Date: 2026-09-05
- Decision: Retain the existing Run Registry as the registry/policy source for governed selection.
- Reason: It preserves existing identity, profile, and policy boundaries without creating a second authority.
- Supersedes: New caller-controlled or replacement registry structures.
- Status: VERIFIED as the current direction; current implementation acceptance is UNVERIFIED.

## Decision 7

- Date: 2026-09-05
- Decision: Keep Loop Detection and No-Progress Detection as separate R&D tracks.
- Reason: Their signals, thresholds, evidence, and action policies are not yet one verified production contract.
- Supersedes: Combining loop and no-progress behavior into an unapproved operational feature.
- Status: VERIFIED as scope control; implementation remains R&D.

## Decision 8

- Date: 2026-09-04
- Decision: Keep failure recovery in the Persistent Execution Harness.
- Reason: Ownership, cancellation, terminal state, and evidence lineage require one bounded recovery boundary.
- Supersedes: Main, adapter, or War Room performing independent recovery.
- Status: VERIFIED for the reported harness direction; current dirty-tree acceptance is UNVERIFIED.
