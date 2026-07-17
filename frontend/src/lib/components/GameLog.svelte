<script lang="ts">
  import type { GameLogEntry } from '$lib/types';

  // The log is ruled lines of the sheet, not a stack of chips: an actor
  // rubric column beside quiet ink, hairlines between entries.
  interface Props {
    entries?: GameLogEntry[];
    activeEntryId?: string | null;
  }

  let { entries = [], activeEntryId = null }: Props = $props();

  function actorLabel(actor: GameLogEntry['actor']): string {
    switch (actor) {
      case 'hero':
        return 'Hero';
      case 'villain':
        return 'Villain';
      default:
        return 'State';
    }
  }

  // Entry text arrives with the actor baked in ("Villain: Cast …"); the
  // rubric column carries the actor now, so the line reads once.
  function lineText(entry: GameLogEntry): string {
    const prefix = `${actorLabel(entry.actor)}: `;
    return entry.text.startsWith(prefix) ? entry.text.slice(prefix.length) : entry.text;
  }
</script>

<section aria-label="Game log">
  <h2 class="mb-1 font-serif text-base font-semibold text-display">Game Log</h2>

  <!-- svelte-ignore a11y_no_noninteractive_tabindex (axe requires keyboard access to scrollable regions) -->
  <div
    class="max-h-[32rem] overflow-y-auto pr-1"
    role="region"
    aria-label="Game log entries"
    tabindex="0"
  >
    {#if entries.length === 0}
      <p class="py-2 text-sm text-ink-2">Actions will appear here.</p>
    {:else}
      <ol class="m-0 list-none p-0">
        {#each entries as entry}
          <li
            data-testid="log-entry"
            class={`entry grid grid-cols-[52px_minmax(0,1fr)] gap-x-2.5 py-1.5 text-sm leading-snug ${entry.id === activeEntryId ? 'active' : ''}`}
          >
            <span class="pt-0.5 font-mono text-[8.5px] font-semibold uppercase tracking-[0.14em] text-ink-2">
              {actorLabel(entry.actor)}
            </span>
            <span>{lineText(entry)}</span>
          </li>
        {/each}
      </ol>
    {/if}
  </div>
</section>

<style>
  .entry + .entry {
    border-top: 1px solid color-mix(in srgb, var(--border) 62%, transparent);
  }
  .entry.active {
    background: var(--bg-field);
    box-shadow: -3px 0 0 0 var(--accent);
  }
</style>
