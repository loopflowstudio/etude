<script lang="ts">
  // Hidden cards wear the classic card back (fetched once — see
  // scripts/fetch-card-art.mjs); when the file is absent the violet
  // material stands in. Like all plates, the back is a fixed world.
  interface Props {
    focused?: boolean;
    clickable?: boolean;
    className?: string;
  }

  let { focused = false, clickable = false, className = '' }: Props = $props();
</script>

<div
  class={`back aspect-[5/7] w-20 overflow-hidden rounded-lg shadow ${focused ? 'ring-1 ring-action' : ''} ${clickable ? 'cursor-pointer' : ''} ${className}`}
></div>

<style>
  .back {
    /* A fixed world keeps a fixed frame: no adaptive tokens inside. */
    border: 1px solid rgb(6 9 15 / 0.55);
    background:
      url('/card-art/card-back.jpg') center / cover no-repeat,
      radial-gradient(circle at 50% 42%, rgb(154 142 190 / 0.22) 0, transparent 46%),
      linear-gradient(152deg, #2a2535 0%, #3d3652 56%, #262130 100%);
  }
</style>
