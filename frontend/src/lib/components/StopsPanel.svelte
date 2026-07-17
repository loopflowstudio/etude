<script lang="ts">
  import { STOP_STEPS } from '$lib/stops';
  import type { StopsConfig, StopSide } from '$lib/types';

  interface Props {
    stops: StopsConfig;
    onToggleStop?: (side: StopSide, step: string) => void;
    onStopOnStackChange?: (value: boolean) => void;
    onAutoPassChange?: (value: boolean) => void;
    onReset?: () => void;
  }

  let {
    stops,
    onToggleStop = undefined,
    onStopOnStackChange = undefined,
    onAutoPassChange = undefined,
    onReset = undefined,
  }: Props = $props();

  const activeCount = $derived(stops.my.length + stops.opponent.length);
  const sides: StopSide[] = ['my', 'opponent'];
</script>

<details class="rounded border border-line bg-panel" data-testid="stops-panel">
  <summary class="cursor-pointer select-none px-4 py-3 text-sm font-semibold uppercase tracking-wide text-ink-2">
    Stops
    <span class="ml-2 rounded bg-panel-muted px-2 py-0.5 text-xs font-normal normal-case text-ink-2">
      {stops.auto_pass ? `${activeCount} set` : 'off'}
    </span>
  </summary>

  <div class="space-y-3 border-t border-line p-4">
    <label class="flex items-center gap-2 text-sm text-ink-2">
      <input
        type="checkbox"
        data-testid="auto-pass-toggle"
        checked={stops.auto_pass}
        onchange={(event) => onAutoPassChange?.(event.currentTarget.checked)}
      />
      Auto-pass priority (MTGO stops)
    </label>

    <div class={stops.auto_pass ? '' : 'pointer-events-none opacity-50'}>
      <div class="grid grid-cols-[minmax(0,1fr)_3rem_3rem] items-center gap-y-1 text-sm">
        <span></span>
        <span class="text-center text-xs font-semibold uppercase text-ink-2">Mine</span>
        <span class="text-center text-xs font-semibold uppercase text-ink-2">Opp</span>
        {#each STOP_STEPS as step}
          <span class="text-ink-2">{step.label}</span>
          {#each sides as side}
            <span class="text-center">
              <input
                type="checkbox"
                data-testid={`stop-${side}-${step.key}`}
                aria-label={`${side === 'my' ? 'My' : 'Opponent'} ${step.label} stop`}
                checked={stops[side].includes(step.key)}
                disabled={!stops.auto_pass}
                onchange={() => onToggleStop?.(side, step.key)}
              />
            </span>
          {/each}
        {/each}
      </div>

      <label class="mt-3 flex items-center gap-2 text-sm text-ink-2">
        <input
          type="checkbox"
          data-testid="stop-on-stack"
          checked={stops.stop_on_stack}
          disabled={!stops.auto_pass}
          onchange={(event) => onStopOnStackChange?.(event.currentTarget.checked)}
        />
        Always stop when the stack is not empty
      </label>
    </div>

    <button
      class="text-xs text-ink-2 underline hover:text-ink"
      data-testid="stops-reset"
      onclick={() => onReset?.()}
    >
      Reset to defaults
    </button>
  </div>
</details>
