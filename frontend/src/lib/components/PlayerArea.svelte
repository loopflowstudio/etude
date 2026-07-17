<script lang="ts">
  import type { CardState, PlayerState } from '$lib/types';

  import Card from './Card.svelte';
  import CardBack from './CardBack.svelte';
  import PermanentRow from './PermanentRow.svelte';

  // A player's region of the sheet: ruled staves with margin rubrics, the
  // way a score names its staves. The opponent reads hand-first (their hand
  // faces you); the hero reads battlefield-first.
  interface Props {
    label: string;
    player: PlayerState;
    deckName?: string | null;
    opponent?: boolean;
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
    player,
    deckName = null,
    opponent = false,
    focusedIds = new Set<number>(),
    clickableTargets = undefined,
    onSelectTarget = undefined,
    onHoverTarget = undefined,
    onPreviewCard = undefined,
  }: Props = $props();

  const hiddenHandCount = $derived(
    player.hand_hidden_count ?? player.zone_counts.HAND ?? player.hand.length,
  );

  // The deck's color identity, drawn as a thin ruled accent under the
  // region heading — the pie as a rule of ink, not a panel.
  const PIE: Record<string, string> = {
    W: 'var(--warning)',
    U: 'var(--info)',
    B: 'var(--neutral)',
    R: 'var(--error)',
    G: 'var(--success)',
  };
  const pieColors = $derived.by(() => {
    const match = /^([WUBRG]{1,5}) /.exec(deckName ?? '');
    return match ? match[1].split('').map((letter) => PIE[letter]) : [];
  });

  function preview(card: CardState): void {
    onPreviewCard?.({
      name: card.name,
      power: card.types.is_creature ? card.power : null,
      toughness: card.types.is_creature ? card.toughness : null,
    });
  }
</script>

{#snippet rubric(text: string)}
  <div class="pt-2 text-right font-mono text-[9px] font-semibold uppercase tracking-[0.16em] text-ink-2">
    {text}
  </div>
{/snippet}

{#snippet playerBar()}
  <!-- The player bar, in the manner of a chess clock plate: identity at
       one end, life at the other. The opponent's sits above their side of
       the table; yours below yours — the players bracket the battlefield. -->
  <div class="rounded-sm border border-line bg-field/70 px-4 py-2">
    <div class="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
      <div class="min-w-0">
        <h2 class="inline font-serif text-lg font-semibold text-display">
          {label}{#if deckName}<span class="text-sm font-normal italic text-ink-2"> — {deckName}</span>{/if}
        </h2>
        {#if pieColors.length > 0}
          <span class="ml-3 inline-flex h-[2px] w-[64px] gap-[4px] align-middle" aria-hidden="true">
            {#each pieColors as color}
              <i class="flex-1" style:background={color}></i>
            {/each}
          </span>
        {/if}
      </div>
      <div class="flex items-baseline gap-4">
        <span class="text-xs italic text-ink-2">Library {player.library_count}</span>
        <div
          class={`flex items-baseline gap-1.5 ${focusedIds.has(player.id) ? 'rounded outline-2 outline-offset-4 outline-action' : ''}`}
        >
          <b class="text-2xl font-semibold leading-none tabular-nums">{player.life}</b>
          <span class="text-[9px] uppercase tracking-[0.2em] text-ink-2">life</span>
        </div>
      </div>
    </div>
  </div>
{/snippet}

<section class="py-3" aria-label={`${label}${deckName ? ` — ${deckName}` : ''}`}>
  {#if opponent}
    {@render playerBar()}
  {/if}

  <div class="staves">
    {#if opponent}
      <div class="staff">
        {@render rubric(`Hand (${hiddenHandCount})`)}
        <div class="flex flex-wrap items-end gap-2">
          {#if hiddenHandCount === 0}
            <div class="px-1 py-3.5 font-serif text-xs italic text-ink-2">Empty hand</div>
          {/if}
          {#each Array(hiddenHandCount) as _, index}
            <div role="img" aria-label={`Hidden card ${index + 1}`}>
              <CardBack />
            </div>
          {/each}
        </div>
      </div>
      <div class="staff">
        {@render rubric('Battlefield')}
        <PermanentRow
          permanents={player.battlefield}
          {focusedIds}
          {clickableTargets}
          {onSelectTarget}
          {onHoverTarget}
          {onPreviewCard}
        />
      </div>
    {:else}
      <div class="staff">
        {@render rubric('Battlefield')}
        <PermanentRow
          permanents={player.battlefield}
          {focusedIds}
          {clickableTargets}
          {onSelectTarget}
          {onHoverTarget}
          {onPreviewCard}
        />
      </div>
      <div class="staff">
        {@render rubric(`Hand (${player.hand.length})`)}
        <div class="flex flex-wrap items-end gap-2">
          {#if player.hand.length === 0}
            <div class="px-1 py-3.5 font-serif text-xs italic text-ink-2">Empty hand</div>
          {/if}
          {#each player.hand as card}
            <Card
              name={card.name}
              power={card.types.is_creature ? card.power : null}
              toughness={card.types.is_creature ? card.toughness : null}
              focused={focusedIds.has(card.id)}
              clickable={clickableTargets?.has(card.id) ?? false}
              onSelect={() => onSelectTarget?.(card.id)}
              onHoverStart={() => {
                onHoverTarget?.(clickableTargets?.has(card.id) ? card.id : null);
                preview(card);
              }}
              onHoverEnd={() => {
                onHoverTarget?.(null);
                onPreviewCard?.(null);
              }}
            />
          {/each}
        </div>
      </div>
    {/if}

    <div class="staff">
      {@render rubric(`Graveyard (${player.graveyard.length})`)}
      <div class="flex flex-wrap items-end gap-2">
        {#if player.graveyard.length === 0}
          <div class="px-1 py-3.5 font-serif text-xs italic text-ink-2">Empty graveyard</div>
        {/if}
        {#each player.graveyard as card}
          <Card
            name={card.name}
            power={card.types.is_creature ? card.power : null}
            toughness={card.types.is_creature ? card.toughness : null}
            focused={focusedIds.has(card.id)}
            spent
            onHoverStart={() => preview(card)}
            onHoverEnd={() => onPreviewCard?.(null)}
          />
        {/each}
      </div>
    </div>

    {#if player.exile.length > 0}
      <div class="staff">
        {@render rubric(`Exile (${player.exile.length})`)}
        <div class="flex flex-wrap items-end gap-2">
          {#each player.exile as card}
            <Card
              name={card.name}
              power={card.types.is_creature ? card.power : null}
              toughness={card.types.is_creature ? card.toughness : null}
              focused={focusedIds.has(card.id)}
              spent
              onHoverStart={() => preview(card)}
              onHoverEnd={() => onPreviewCard?.(null)}
            />
          {/each}
        </div>
      </div>
    {/if}
  </div>

  {#if !opponent}
    {@render playerBar()}
  {/if}
</section>

<style>
  .staves .staff {
    display: grid;
    grid-template-columns: 88px minmax(0, 1fr);
    column-gap: 18px;
    align-items: end;
    padding: 10px 0 12px;
  }
  .staves .staff + .staff {
    border-top: 1px solid color-mix(in srgb, var(--border) 62%, transparent);
  }
  @media (max-width: 640px) {
    .staves .staff {
      grid-template-columns: 1fr;
      row-gap: 6px;
    }
    .staves .staff > :first-child {
      padding-top: 6px;
      text-align: left;
    }
  }
</style>
