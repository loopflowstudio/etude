<script lang="ts">
  import type { Snippet } from 'svelte';

  import '../app.css';

  let { children }: { children?: Snippet } = $props();
</script>

<svelte:head>
  <title>Etude Fantasia</title>
</svelte:head>

<div class="min-h-screen bg-slate-900 text-slate-100">
  <!-- The banner carries the color pie as a W→U→R→B→G weave: each color
       radiates from its own point, alternating edges, in the manner of the
       loopflow logo's multi-direction gradient. -->
  <header
    class="border-b border-slate-800"
    style="background:
        radial-gradient(ellipse 40% 160% at 2% 0%, color-mix(in srgb, var(--warning) 38%, transparent), transparent 70%),
        radial-gradient(ellipse 40% 160% at 26% 100%, color-mix(in srgb, var(--info) 38%, transparent), transparent 70%),
        radial-gradient(ellipse 40% 160% at 50% 0%, color-mix(in srgb, var(--accent) 38%, transparent), transparent 70%),
        radial-gradient(ellipse 40% 160% at 74% 100%, color-mix(in srgb, var(--neutral) 38%, transparent), transparent 70%),
        radial-gradient(ellipse 40% 160% at 98% 0%, color-mix(in srgb, var(--success) 38%, transparent), transparent 70%),
      var(--bg-surface)"
  >
    <div class="mx-auto flex w-full max-w-[1600px] items-center justify-between px-4 py-3">
      <div class="leading-none">
        <div data-testid="brand-name" class="font-serif text-2xl font-semibold tracking-[-0.01em] text-accent-text">Etude Fantasia</div>
        <div class="mt-1 font-mono text-[9px] uppercase tracking-[0.18em] text-slate-400">Play · replay · study</div>
      </div>
      <nav class="flex items-center gap-3 text-sm text-slate-300">
        <a class="rounded border border-transparent px-3 py-2 hover:border-slate-700 hover:bg-slate-800 hover:text-accent-text" href="/">Play</a>
        <a class="rounded border border-transparent px-3 py-2 hover:border-slate-700 hover:bg-slate-800 hover:text-accent-text" href="/replay">Replay</a>
      </nav>
    </div>
  </header>

  {@render children?.()}
</div>
