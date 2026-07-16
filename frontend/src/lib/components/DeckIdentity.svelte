<script lang="ts">
  // Deck names lead with their color identity ("UR Lessons"); present those
  // letters as mana discs in the pie's family colors. The letters stay real
  // text so the rendered name reads identically to the plain string.
  const FAMILY: Record<string, string> = {
    W: 'bg-amber-600',
    U: 'bg-indigo-500',
    B: 'bg-purple-500',
    R: 'bg-rose-500',
    G: 'bg-emerald-600',
  };

  interface Props {
    name: string;
  }

  let { name }: Props = $props();

  const parsed = $derived(/^([WUBRG]{1,5}) (.+)$/.exec(name));
  const colors = $derived(parsed ? parsed[1].split('') : []);
  const rest = $derived(parsed ? parsed[2] : name);
</script>

{#if colors.length > 0}<span class="inline-flex items-center gap-0.5 align-[-2px]"
  >{#each colors as color}<span
      class={`inline-flex h-3.5 w-3.5 items-center justify-center rounded-full text-[9px] font-bold leading-none text-white ${FAMILY[color]}`}
      >{color}</span
    >{/each}</span
> {rest}{:else}{name}{/if}
