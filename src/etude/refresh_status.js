// Visible tabs check the index. A waiting report stops polling once its files arrive.
(() => {
  const {id, dataset: {refresh, version}} = document.querySelector("[data-refresh]");
  const path = version ? location.pathname + "?observe=1" : location.pathname.replace(/\/$/, "") + "/status";
  const error = document.getElementById("status-error");

  async function check() {
    let navigating = false;
    try {
      if (document.hidden) return;
      const response = await fetch(path);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const template = document.createElement("template");
      template.innerHTML = await response.text();
      const next = template.content.firstElementChild;
      if (next?.id !== id || (version && !next.dataset.version)) throw new Error("Invalid observation");
      if (document.hidden) return;

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
      } else {
        const link = document.activeElement;
        const href = current.contains(link) && link.getAttribute("href");
        current.replaceWith(next);
        if (href) next.querySelector(`a[href=${CSS.escape(href)}]`)?.focus({preventScroll: true});
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
