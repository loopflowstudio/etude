# v0.4.6

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.4.6 gives you one browser view of live experiments, from training curves and console output to the machines and rental windows behind each run. Training recipes can now resume with their own checkpoint files. The launcher keeps long Colab runs connected and can move on when an accelerator has no capacity.

## Follow runs and rentals in one place

Run `etude view` to open a live dashboard at `http://127.0.0.1:8765`. It combines run files from a directory or S3/R2 bucket with launcher state from a local or SSH launcher. While a local view is running, `train`, `launch`, `queue`, `continue` and `status RUN` print clickable `view:` links. [#135](https://github.com/loopflowstudio/etude/pull/135)

- Open a run to see its charts, console, config, checkpoints and files. Browse experiment reports or check held machines, rental windows, costs and occupied slots. Open pages refresh in place every five seconds.
- On large buckets, run metadata appears before historical charts, which are built when you open a run. The view keeps the last valid results visible through a storage outage, launcher disconnect or syntax error in an edited `report()`.
- Use `etude view` in place of `etude report --serve`. For a self-contained, printable HTML report, use `etude report`. It still supports `--watch`.

## Resume training with your framework’s files

Pass both `export(folder)` and `restore(folder)` to `run.resume()`. Each scheduled save, completion save or planned-stop save can include custom files alongside any PyTorch objects and typed JSON counters you supply. A fresh run skips the restore callback. [#141](https://github.com/loopflowstudio/etude/pull/141), [#143](https://github.com/loopflowstudio/etude/pull/143)

- Exporters can create subdirectories. Delivery uploads the selected save’s files, including nested files and files ending in `.tail`, before publishing `current.json`. Follow that pointer to find the committed save. A failed export leaves the previous save selected.
- Manual restore with `path = run.resume()` and explicit saves with `run.save(export=fn)` still work. `run.resume()` returns the supplied `extra` counters when present and a path or `None` otherwise. The names `export` and `restore` are now reserved on that method, and supplying just one callback raises `ValueError`.

## Keep Colab training connected

Colab reclaimed some busy runtimes after about 21 minutes without a connected client. Etude now keeps a Jupyter connection open, sends a heartbeat during training and renews the connection as proxy credentials rotate. An L4 training run passed 76 minutes during verification. [#142](https://github.com/loopflowstudio/etude/pull/142)

- When Colab returns HTTP 503 during assignment, Etude checks whether a runtime was created. If none exists, it treats the response as a capacity refusal so scheduling can move on before the rental deadline.
- If bootstrap exits early and leaves the kernel idle, Etude detects the loss before the startup allowance runs out.

## Operational notes

Run `etude database upgrade` on an existing launcher database before using the upgraded launcher. The migration adds indexes for reading machine inventory from one snapshot. The live view’s index under `ETUDE_HOME/views` can be rebuilt from its sources. [#135](https://github.com/loopflowstudio/etude/pull/135)

## Small changes

- Grouped evaluations keep their submitted run address through delivery and recovery. Continuations show their own launcher state and link to their parent from submission. [#135](https://github.com/loopflowstudio/etude/pull/135)