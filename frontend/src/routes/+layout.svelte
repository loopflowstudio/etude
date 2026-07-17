<script lang="ts">
  import type { Snippet } from 'svelte';

  import '../app.css';

  let { children }: { children?: Snippet } = $props();
</script>

<svelte:head>
  <title>Etude Fantasia</title>
</svelte:head>

<div class="min-h-screen text-ink">
  <!-- The banner carries the color pie as a W→U→R→B→G weave: each color
       radiates from its own point, alternating edges, in the manner of the
       loopflow logo's multi-direction gradient. Like the card name plates,
       the banner is a fixed rich world in both modes — Lotus Cobra
       saturation under a title scrim, literal ivory text. -->
  <header
    class="border-b border-black/30"
    style="background:
        linear-gradient(180deg, rgb(15 11 6 / 0.34), rgb(15 11 6 / 0.2)),
        radial-gradient(ellipse 42% 170% at 2% 0%, rgb(242 173 37 / 0.85), transparent 78%),
        radial-gradient(ellipse 42% 170% at 26% 100%, rgb(47 116 208 / 0.85), transparent 78%),
        radial-gradient(ellipse 42% 170% at 50% 0%, rgb(224 74 24 / 0.85), transparent 78%),
        radial-gradient(ellipse 42% 170% at 74% 100%, rgb(149 80 180 / 0.85), transparent 78%),
        radial-gradient(ellipse 42% 170% at 98% 0%, rgb(70 162 48 / 0.85), transparent 78%),
      #221a10"
  >
    <div class="mx-auto flex w-full max-w-[1600px] items-center justify-between px-4 py-3">
      <div class="leading-none">
        <div data-testid="brand-name" class="font-serif text-3xl font-semibold tracking-[-0.01em] text-[#f8f1e0]">Etude Fantasia</div>
        <div class="mt-1 font-mono text-[9px] uppercase tracking-[0.18em] text-[#f8f1e0]/65">Play · replay · study</div>
      </div>
      <nav class="flex items-center gap-3 text-sm text-[#f8f1e0]/85">
        <a class="rounded border border-transparent px-3 py-2 hover:border-[#f8f1e0]/25 hover:bg-[#f8f1e0]/10 hover:text-[#f8f1e0]" href="/">Play</a>
        <a class="rounded border border-transparent px-3 py-2 hover:border-[#f8f1e0]/25 hover:bg-[#f8f1e0]/10 hover:text-[#f8f1e0]" href="/replay">Replay</a>
      </nav>
    </div>
  </header>

  {@render children?.()}
</div>
