<script lang="ts">
  import CardImage from './CardImage.svelte';

  interface Props {
    name: string;
    power?: number | null;
    toughness?: number | null;
    focused?: boolean;
    clickable?: boolean;
    tapped?: boolean;
    dimmed?: boolean;
    damage?: number;
    size?: 'small' | 'normal';
    onSelect?: () => void;
    onHoverStart?: () => void;
    onHoverEnd?: () => void;
  }

  let {
    name,
    power = null,
    toughness = null,
    focused = false,
    clickable = false,
    tapped = false,
    dimmed = false,
    damage = 0,
    size = 'small',
    onSelect = undefined,
    onHoverStart = undefined,
    onHoverEnd = undefined,
  }: Props = $props();

  const widthClass = $derived(size === 'normal' ? 'w-40' : 'w-20');
  const imageClass = 'h-full w-full rounded-md object-cover';

  // A tapped card rotates in place, but its flex slot stays portrait-sized,
  // so without compensation the rotated card overhangs (height - width) / 2
  // on each side and shingles under its neighbours. The margin restores the
  // rotated footprint: 16px for w-20/h-28 cards, 32px for w-40/h-56.
  const tappedClass = $derived(size === 'normal' ? 'rotate-90 mx-8' : 'rotate-90 mx-4');

  // Tapped, summoning-sick, and damage states are shown by rotation, opacity,
  // and badge color; the accessible name must carry them too.
  const stateNotes = $derived(
    [
      tapped ? 'tapped' : null,
      dimmed ? 'summoning sick' : null,
      damage > 0 ? `${damage} damage` : null,
    ].filter((note): note is string => note !== null),
  );
  const accessibleName = $derived(
    stateNotes.length > 0 ? `${name} (${stateNotes.join(', ')})` : name,
  );
</script>

<button
  type="button"
  aria-label={accessibleName}
  aria-disabled={!clickable}
  tabindex={clickable ? 0 : -1}
  class={`group relative aspect-[5/7] ${widthClass} overflow-visible rounded-lg border bg-slate-900 text-left shadow transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-400 ${focused ? 'border-blue-400 ring-1 ring-blue-400/70' : 'border-slate-700'} ${clickable ? 'cursor-pointer hover:-translate-y-1 hover:border-amber-300' : 'cursor-default'} ${tapped ? tappedClass : ''} ${dimmed ? 'opacity-70' : ''}`}
  onclick={() => {
    if (clickable) {
      onSelect?.();
    }
  }}
  onmouseenter={() => onHoverStart?.()}
  onmouseleave={() => onHoverEnd?.()}
  onfocus={() => onHoverStart?.()}
  onblur={() => onHoverEnd?.()}
>
  <div class="absolute inset-0 overflow-hidden rounded-lg">
    <CardImage name={name} size={size} alt={name} className={imageClass}>
      <div class="flex h-full w-full flex-col justify-between bg-slate-800 p-2 text-left text-[11px] text-slate-200">
        <div class="font-semibold leading-tight">{name}</div>
        {#if power !== null && toughness !== null}
          <div class="self-end rounded bg-slate-950/80 px-1.5 py-0.5 text-[10px] font-semibold">
            {power}/{toughness}
          </div>
        {/if}
      </div>
    </CardImage>
  </div>

  <div class="pointer-events-none absolute inset-0 rounded-lg ring-1 ring-inset ring-black/20"></div>

  {#if power !== null && toughness !== null}
    <div class="pointer-events-none absolute bottom-1 right-1 rounded bg-[rgb(10_13_20/0.9)] px-1.5 py-0.5 text-[10px] font-semibold text-[#faf8f5]">
      {power}/{toughness}
    </div>
  {/if}

  {#if damage > 0}
    <div class="pointer-events-none absolute right-1 top-1 rounded-full bg-rose-600 px-2 py-0.5 text-[10px] font-semibold text-white">
      {damage}
    </div>
  {/if}
</button>
