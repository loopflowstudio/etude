# Questions and assumptions

- The broad `tests/sim` run passes 116 tests and fails only
  `test_int4_contract_binds_runtime_and_both_execution_profiles`. That test
  compares the current Rust source, rebuilt extension, and content manifest to
  the immutable INT-4 pre-registration, so RUL-2's intentional runtime change
  is expected to fail it. The approved RUL-2 design says frozen INT-4 artifacts
  must not be rewritten; this branch therefore leaves the INT-4 contract and
  receipt untouched and validates the new runtime through the independently
  frozen and verified RUL-2 contract instead.
