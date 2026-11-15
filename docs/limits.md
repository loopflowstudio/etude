# Limits and sharp edges

This page helps prototype authors decide whether Etude fits their workload and understand the boundaries of its recovery and cost controls.

- **Your algorithm stays yours.** Etude supplies neither a trainer nor game
  rules, search, sampling, an LLM client or a hosted model endpoint. Write those
  in your training or player code. It does not schedule distributed model training.
- **Inference sharing is within one experiment allocation.** Compatible jobs
  share checkpoint forwards on one machine. Cross-experiment serving and remote
  hosted-player orchestration are outside this API. Etude chooses direct CPU or shared GPU inference from visible hardware;
  your loader still selects the model device.
- **Sharing is not always faster.** Etude therefore loads CPU models in each game process. The shared Large GPU layout beat the frozen Manabot baseline in
  four fair pairs on one 4090, but that is not a universal throughput guarantee.
  [Measurements](dev/evaluation.md#agents) retain the evidence and limitations.
- **Checkpoint size is not GPU memory use.** `Agents.memory_gb` accounts for
  checkpoint-file bytes; your loader owns device allocation. A visible NVIDIA
  GPU lets active games fill the batch; memory and pending work may reduce it.
- **Recovery restores registered state.** Random streams and games in flight
  survive only if you save them. A killed process can lose work since its last
  save or sealed output. Keep checkpoint formats compatible across attempts.
- **Results must be valid.** Raise for an invalid game. Empty/error-shaped tables
  are not successes. Ordinary failures require `--retry-failed`; transient
  `RetryRequested` failures remain eligible for another pass.
- **Costs are estimates until settled.** Rental deadlines and dollar caps bound
  intended compute windows, not provider outages, delayed deletion, disk charges
  or bucket/API bills. Keep the scheduler alive and inspect cleanup status.
- **Credentials do not sandbox jobs.** Job code is trusted code running on the
  host. Do not treat an allocation as isolation from hostile authors.
- **Concurrency changes batches.** A forward must be deterministic and independent
  of batch companions. Etude cannot prove numerical equivalence. Seed player
  randomness from `match.seed`, and test your own model under different batches.

[Training](training.md), [evaluation](evaluation.md), [tracking](tracking.md) and
[machines](launcher.md) explain the controls for each boundary.
