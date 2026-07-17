<script lang="ts">
  interface Props {
    focused?: boolean;
    clickable?: boolean;
    className?: string;
  }

  let { focused = false, clickable = false, className = '' }: Props = $props();
</script>

<div
  class={`aspect-[5/7] w-20 rounded-lg border bg-gradient-to-br from-slate-700 via-slate-800 to-slate-950 p-2 text-center shadow ${focused ? 'border-blue-400 ring-1 ring-blue-400/70' : 'border-slate-600'} ${clickable ? 'cursor-pointer hover:border-amber-300' : ''} ${className}`}
>
  <!-- Hidden cards are material, not labels: the Swamp-violet frame says it. -->
  <div class="h-full rounded-md border border-purple-500/40 bg-purple-900/15"></div>
</div>
