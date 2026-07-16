<script lang="ts">
  // Deck names lead with their color identity ("UR Lessons"); present those
  // letters as mana pips in the classic pale mana-symbol colors (fixed
  // literals, like card art — mana pips do not theme). The letters stay
  // real text so the rendered name reads identically to the plain string.
  const PIP: Record<string, string> = {
    W: '#f0f2c0',
    U: '#b5cde3',
    B: '#aca29a',
    R: '#db8664',
    G: '#93b483',
  };

  interface Props {
    name: string;
    symbolsOnly?: boolean;
  }

  let { name, symbolsOnly = false }: Props = $props();

  const parsed = $derived(/^([WUBRG]{1,5}) (.+)$/.exec(name));
  const colors = $derived(parsed ? parsed[1].split('') : []);
  const rest = $derived(parsed ? parsed[2] : name);
</script>

{#if colors.length > 0}<span class="inline-flex items-center gap-1"
  ><span class="inline-flex items-center gap-0.5"
    >{#each colors as color}<span
        class="inline-flex h-[15px] w-[15px] items-center justify-center rounded-full text-[9px] font-bold leading-none text-black/75 shadow-[-1px_1px_0_rgba(0,0,0,0.3)]"
        style:background={PIP[color]}>{color}</span
      >{/each}</span
  >{#if !symbolsOnly}<span>{' '}{rest}</span>{/if}</span
>{:else if !symbolsOnly}{name}{/if}
