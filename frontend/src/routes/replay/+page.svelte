<script lang="ts">
  import { onMount, untrack } from 'svelte';

  import GameBoard from '$lib/components/GameBoard.svelte';
  import GameLog from '$lib/components/GameLog.svelte';
  import Timeline from '$lib/components/Timeline.svelte';
  import { createReplayStore } from '$lib/replay.svelte';
  import { replayLogEntries } from '$lib/replay';
  import { createPresentationPlayer } from '$lib/presentation.svelte';
  import type { Trace, TraceSummary } from '$lib/types';

  const replayStore = createReplayStore();
  const presentationPlayer = createPresentationPlayer();

  onMount(() => {
    void loadTraces();
  });

  $effect(() => {
    if (!replayStore.playing) {
      return;
    }
    const timer = setInterval(() => {
      // A replay frame owns all of its ordered semantic beats. Do not replace
      // them with the next authoritative frame mid-sequence.
      if (!presentationPlayer.currentEvent) {
        replayStore.tick();
      }
    }, Math.max(1000 / replayStore.speed, 100));
    return () => clearInterval(timer);
  });

  const currentFrame = $derived(replayStore.frames[replayStore.currentFrameIndex] ?? null);
  const logEntries = $derived(replayStore.trace ? replayLogEntries(replayStore.trace) : []);
  const activeLogEntryId = $derived(
    replayStore.currentFrameIndex > 0
      ? logEntries[replayStore.currentFrameIndex - 1]?.id ?? null
      : null,
  );

  $effect(() => {
    if (!currentFrame) {
      untrack(() => presentationPlayer.recover());
      return;
    }
    untrack(() =>
      presentationPlayer.recover(
        currentFrame.presentation,
        currentFrame.presentationLabels,
      ),
    );
  });

  async function loadTraces(): Promise<void> {
    replayStore.setLoadingList(true);
    replayStore.setError(null);

    try {
      const response = await fetch('/api/traces');
      if (!response.ok) {
        throw new Error(`Failed to load traces (${response.status})`);
      }
      const payload = (await response.json()) as TraceSummary[];
      replayStore.setSummaries(payload);
    } catch (error) {
      replayStore.setError(error instanceof Error ? error.message : 'Failed to load traces.');
    } finally {
      replayStore.setLoadingList(false);
    }
  }

  async function loadTrace(traceId: string): Promise<void> {
    replayStore.setLoadingTrace(true);
    replayStore.setError(null);

    try {
      const response = await fetch(`/api/traces/${traceId}`);
      if (!response.ok) {
        throw new Error(`Failed to load trace ${traceId} (${response.status})`);
      }
      const payload = (await response.json()) as Trace;
      replayStore.loadTrace(payload);
    } catch (error) {
      replayStore.setError(error instanceof Error ? error.message : 'Failed to load trace.');
    } finally {
      replayStore.setLoadingTrace(false);
    }
  }

  function winnerLabel(winner: number | null): string {
    if (winner === null) {
      return 'Draw';
    }
    return winner === 0 ? 'Hero' : 'Opponent';
  }
</script>

<main class="mx-auto w-full max-w-[1600px] p-4">
  <div class="grid grid-cols-1 gap-4 xl:grid-cols-[320px_minmax(0,1fr)_320px]">
    <section class="rounded border border-line bg-panel p-4">
      <div class="mb-3 flex items-center justify-between gap-3">
        <h1 class="text-lg font-bold text-display">Replay</h1>
        <button class="rounded border border-line-strong bg-field px-3 py-2 text-sm hover:border-action" onclick={() => void loadTraces()}>
          Refresh
        </button>
      </div>

      {#if replayStore.loadingList}
        <p class="text-sm text-ink-2">Loading traces…</p>
      {:else if replayStore.summaries.length === 0}
        <p class="text-sm text-ink-2">No traces yet. Play a game first.</p>
      {:else}
        <div class="space-y-2">
          {#each replayStore.summaries as summary}
            <button
              class={`w-full rounded border px-3 py-3 text-left text-sm ${replayStore.trace?.id === summary.id ? 'border-action bg-field' : 'border-line bg-field/60 hover:border-line-strong'}`}
              onclick={() => void loadTrace(summary.id)}
            >
              <div class="font-mono text-xs text-ink">{summary.id}</div>
              <div class="mt-1 text-xs text-ink-2">{summary.timestamp ?? 'Unknown time'}</div>
              <div class="mt-1 text-xs text-ink-2">
                Winner: {winnerLabel(summary.winner)} · Events: {summary.num_events}
              </div>
            </button>
          {/each}
        </div>
      {/if}
    </section>

    <div class="space-y-4">
      {#if replayStore.errorMessage}
        <section class="rounded border border-mountain/50 bg-mountain/20 px-4 py-3 text-sm text-mountain-ink">
          {replayStore.errorMessage}
        </section>
      {/if}

      {#if replayStore.loadingTrace}
        <section class="rounded border border-line bg-panel p-10 text-center text-ink-2">
          Loading replay…
        </section>
      {:else if currentFrame && replayStore.trace}
        <div class="space-y-4">
          <Timeline
            currentFrame={replayStore.currentFrameIndex}
            totalFrames={replayStore.frames.length}
            playing={replayStore.playing}
            speed={replayStore.speed}
            actionDescription={currentFrame.actionDescription}
            actor={currentFrame.actor}
            onPrevious={() => replayStore.previousFrame()}
            onNext={() => replayStore.nextFrame()}
            onTogglePlaying={() => replayStore.togglePlaying()}
            onScrub={(index) => replayStore.setFrame(index)}
            onSpeedChange={(speed) => replayStore.setSpeed(speed)}
          />

          <GameBoard
            observation={currentFrame.observation}
            focusedIds={new Set()}
            winner={currentFrame.observation.game_over ? replayStore.trace.winner : undefined}
            {presentationPlayer}
          />
        </div>
      {:else}
        <section class="rounded border border-line bg-panel p-10 text-center text-ink-2">
          Select a trace to inspect it.
        </section>
      {/if}
    </div>

    <GameLog entries={logEntries} activeEntryId={activeLogEntryId} />
  </div>
</main>
