// Visible tabs refresh the rail and the page from one observation.
(() => {
  const hasRail = Boolean(document.getElementById("view-rail"));
  const {id, dataset: {version, refresh}} = document.querySelector("[data-refresh]:not(#view-rail)")
    || document.getElementById("view-rail");
  const railOnly = id === "view-rail";
  const error = document.getElementById("status-error");
  const path = railOnly || version
    ? location.pathname + (hasRail ? "?observe=1" : "?observe=1&embed=1")
    : location.pathname.replace(/\/$/, "") + "/status";

  function replace(current, next) {
    const focus = document.activeElement;
    const href = current.contains(focus) && focus.getAttribute("href");
    const {scrollLeft, scrollTop} = current;
    current.replaceWith(next);
    if (href) next.querySelector(`a[href=${CSS.escape(href)}]`)?.focus({preventScroll: true});
    next.scrollTo(scrollLeft, scrollTop);
  }

  const cards = id === "experiment-status" && typeof drawExperimentCards === "function";
  const drawCards = () => drawExperimentCards(document.getElementById(id));
  if (cards) {
    drawCards();
    let frame;
    window.addEventListener("resize", () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(drawCards);
    });
  }

  async function check() {
    let navigating = false;
    try {
      if (document.hidden) return;
      const response = await fetch(path);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const template = document.createElement("template");
      template.innerHTML = await response.text();
      const next = template.content.firstElementChild;
      if (!railOnly && (next?.id !== id || (version && !next.dataset.version))) throw new Error("Invalid observation");
      if (document.hidden) return;

      if (hasRail) {
        const nav = next.nextElementSibling;
        if (nav?.id !== "view-rail") throw new Error("Missing navigation");
        replace(document.getElementById("view-rail"), nav);
      }
      const current = document.getElementById(id);
      if (version) {
        current.textContent = next.textContent;
        if (next.dataset.version !== version) {
          location.reload();
          navigating = true;
          return;
        }
        const collected = document.getElementById("console-collected");
        if (collected) {
          collected.textContent = next.dataset.collected;
          collected.hidden = !next.dataset.collected;
        }
      } else if (!railOnly) {
        replace(current, next);
        if (cards) drawCards();
      }
      error.textContent = "";
    } catch (cause) {
      error.textContent = `Status refresh failed: ${cause.message}. Showing the last observation.`;
    } finally {
      if (!navigating) setTimeout(check, Number(refresh));
    }
  }
  setTimeout(check, Number(refresh));
})();
