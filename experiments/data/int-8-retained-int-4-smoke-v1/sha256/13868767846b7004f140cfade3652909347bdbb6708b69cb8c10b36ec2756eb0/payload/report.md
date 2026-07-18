# INT-4 Visit Teacher Iteration

- Profile: `smoke`
- Status: `completed`
- Verdict: `revise` — engineering_smoke_only_no_admission_claim
- Contract SHA-256: `9c3f0f600b70ca4fef7131086f6d9b350f9612e690cbb9d621e961a5de27d03c`
- Runtime source SHA-256: `14a3c5ff6594ad3c354abd06a9e888a3ad2c2d6e2d741db8954efa4f68c89dea`

## Teacher versus random

| Traversals | Games | Win rate | p50 ms | p95 ms | Labels/s |
|---:|---:|---:|---:|---:|---:|
| 4 | 2 | 1.000 | 1.59 | 3.22 | 593.74 |
| 8 | 2 | 0.500 | 3.35 | 5.74 | 294.27 |

## Dataset and replay

- Games: 4
- Decisions: 507
- Exact trajectory/search replay: `True`
- Learner/audit mismatches: 0

## Student validation

| Seed | Arm | Policy CE | Policy KL | Root-value Brier | Checkpoint |
|---:|---|---:|---:|---:|---|
| 197 | chosen_policy_only | 0.9040 | 0.9040 | 0.1055 | `9004b87e2be4a893` |
| 197 | chosen_policy_value | 0.9045 | 0.9045 | 0.0991 | `1c31b6c4f76ad86f` |
| 197 | visit_policy_only | 0.9064 | 0.1430 | 0.1055 | `c2c8dcec02dbcf19` |
| 197 | visit_policy_value | 0.9066 | 0.1432 | 0.0990 | `b97a4796a6cbcad0` |

## Four-agent arena

### Seed 197

| Cell | Games | Hero win rate |
|---|---:|---:|
| teacher-vs-policy-only | 2 | 1.000 |
| teacher-vs-student | 2 | 0.500 |
| teacher-vs-student+search | 2 | 1.000 |
| policy-only-vs-student | 2 | 0.500 |
| policy-only-vs-student+search | 2 | 1.000 |
| student-vs-student+search | 2 | 1.000 |

## Interpretation

This vertical slice does not yet execute the frozen Teacher-0 incumbent or competency cells.
The smoke profile validates plumbing and integrity only; its game-level rates are not method-level evidence.
