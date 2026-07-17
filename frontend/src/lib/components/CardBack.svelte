<script lang="ts">
  // Hidden cards are material, not labels: a Swamp-violet back with a
  // blind lozenge. Like card art, the back is a fixed world in both modes.
  interface Props {
    focused?: boolean;
    clickable?: boolean;
    className?: string;
  }

  let { focused = false, clickable = false, className = '' }: Props = $props();
</script>

<div
  class={`back aspect-[5/7] w-20 rounded-lg p-1 shadow ${focused ? 'ring-1 ring-action' : ''} ${clickable ? 'cursor-pointer' : ''} ${className}`}
>
  <div class="grid h-full place-items-center rounded-md">
    <i class="lozenge" aria-hidden="true"></i>
  </div>
</div>

<style>
  .back {
    border: 1px solid color-mix(in srgb, var(--neutral) 62%, var(--border));
    background:
      radial-gradient(circle at 50% 42%, rgb(154 142 190 / 0.22) 0, transparent 46%),
      linear-gradient(152deg, #2a2535 0%, #3d3652 56%, #262130 100%);
  }
  .lozenge {
    width: 12px;
    height: 12px;
    transform: rotate(45deg);
    border: 1px solid rgb(196 188 222 / 0.55);
  }
</style>
