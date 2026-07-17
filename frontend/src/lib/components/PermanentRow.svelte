<script lang="ts">
  import type { PermanentState } from '$lib/types';

  import Card from './Card.svelte';

  interface Props {
    label: string;
    permanents?: PermanentState[];
    focusedIds?: Set<number>;
    clickableTargets?: Map<number, number[]>;
    onSelectTarget?: (objectId: number) => void;
    onHoverTarget?: (objectId: number | null) => void;
    onPreviewCard?: (
      card: { name: string | null; power: number | null; toughness: number | null } | null,
    ) => void;
  }

  let {
    label,
    permanents = [],
    focusedIds = new Set<number>(),
    clickableTargets = undefined,
    onSelectTarget = undefined,
    onHoverTarget = undefined,
    onPreviewCard = undefined,
  }: Props = $props();
</script>

<div class="min-h-24">
  <div class="mb-2 text-xs uppercase tracking-wide text-slate-400">{label}</div>
  <div class="flex flex-wrap gap-3">
    {#if permanents.length === 0}
      <div class="px-1 py-2 text-xs text-slate-400">No permanents</div>
    {/if}

    {#each permanents as permanent}
      <Card
        name={permanent.name ?? 'Unknown Permanent'}
        power={permanent.power === 0 && permanent.toughness === 0 ? null : permanent.power}
        toughness={permanent.power === 0 && permanent.toughness === 0 ? null : permanent.toughness}
        focused={focusedIds.has(permanent.id)}
        clickable={clickableTargets?.has(permanent.id) ?? false}
        tapped={permanent.tapped}
        dimmed={permanent.summoning_sick}
        counters={permanent.plus1_counters}
        damage={permanent.damage}
        onSelect={() => onSelectTarget?.(permanent.id)}
        onHoverStart={() => {
          onHoverTarget?.(clickableTargets?.has(permanent.id) ? permanent.id : null);
          onPreviewCard?.({
            name: permanent.name,
            power: permanent.power === 0 && permanent.toughness === 0 ? null : permanent.power,
            toughness:
              permanent.power === 0 && permanent.toughness === 0 ? null : permanent.toughness,
          });
        }}
        onHoverEnd={() => {
          onHoverTarget?.(null);
          onPreviewCard?.(null);
        }}
      />
    {/each}
  </div>
</div>
