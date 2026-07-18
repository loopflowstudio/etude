# INT-7 Value Target Comparison

Decision: **continue_visit_policy_only**
Point-estimate winner: `visit_policy_only`

This is one-corpus engineering-smoke evidence only. It is not an admission, promotion, rating, strength, or method claim.

## Complete-player results

| Method | Mean diagnostic rating | Anchor paired score | Competency correct / 30 | Mean p95 (s) |
| --- | ---: | ---: | ---: | ---: |
| `visit_policy_only` | 1361.172 | 0.533 | 0 | 0.062623 |
| `visit_terminal` | 1032.013 | 0.250 | 1 | 0.058447 |
| `visit_blend_50_50` | 930.997 | 0.117 | 1 | 0.060593 |
| `visit_teacher_root` | 1039.921 | 0.233 | 0 | 0.058366 |

## Cost

Inherited labels: 507.
Marginal training seconds: 63.665.
Evaluation wall seconds: 2417.404.

Calibration and teacher-target agreement are retained as subordinate diagnostics and did not select the winner.
