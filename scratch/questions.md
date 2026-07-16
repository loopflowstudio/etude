# Open questions and assumptions — W2-198 compact clone plus undo

Headless run; decisions made inline rather than blocking.

## Decisions taken

1. **Sequential siblings are taken with `mark`/`rollback`, not a fork per
   simulation.** The registered workload dimensions (worlds, rollouts, actions,
   seeds, max_steps) are unchanged; only the branch mechanism differs, which is
   exactly what the candidate is. `FullCloneDriver::mark` is a whole-`Game`
   clone, so the baseline does the same work it did before, and
   `result_checksum` is identical across candidates on every cell — that
   equality is the evidence the cells are matched. Had I left the fork-per-sim
   loop in place, the undo journal would never have been exercised and the
   sequential cells would have measured nothing.

2. **Retained cells keep forking.** Concurrently live slots each need an exact
   copy; an undo journal only reverses one sequential branch at a time. This
   matches the preregistration's expectation that retained cells are a wash and
   exist to price journal overhead.

3. **Both candidates' artifacts are excluded from `source_sha256`.** Two
   matched artifacts must be able to coexist on one tree; otherwise generating
   the second invalidates the first's source digest. This changes the digest
   relative to the old W2-182 artifact, which had to be regenerated regardless
   (see below).

4. **Analysis prose lives in the PR body, not a new tracked doc.** Any file
   added after the benchmark runs would change the tree and invalidate
   `source_sha256`. The generated reports carry the numbers.

## Pre-existing breakage repaired

`verify` failed on merged main with "contract digest mismatch": the
SearchStateWitness refactor edited the contract doc and `benchmark.rs` but the
checked-in W2-182 artifact was never regenerated. Not introduced here;
regenerated at the final tree so `verify` is green at landing.

## Not done, deliberately (serial gate)

Dense page-COW is W2-199 and is not implemented or selected here. This task
produces the matched evidence W2-199 is gated on; it does not choose a
branching representation.
