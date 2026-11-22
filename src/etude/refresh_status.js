// The report frame keeps its current build, selections and scroll position.
async function refreshStatus() {
  const status = document.getElementById("experiment-status");
  if (!document.hidden) {
    const error = document.getElementById("status-error");
    try {
      const response = await fetch(location.pathname.replace(/\/$/, "") + "/status");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const html = await response.text();
      const link = document.activeElement;
      const href = status.contains(link) && link.getAttribute("href");
      status.outerHTML = html;
      if (href) document.querySelector(`#experiment-status a[href=${CSS.escape(href)}]`)?.focus({preventScroll: true});
      error.textContent = "";
    } catch (cause) {
      error.textContent = `Status refresh failed: ${cause.message}. Showing the last observation.`;
    }
  }
  scheduleStatus();
}
function scheduleStatus() {
  const delay = document.getElementById("experiment-status").dataset.refresh;
  if (delay) setTimeout(refreshStatus, Number(delay));
}
scheduleStatus();
