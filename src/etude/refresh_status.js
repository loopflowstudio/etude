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
    const inside = current.contains(focus);
    const href = inside && focus.getAttribute("href");
    const focusedId = inside && focus.id;
    const {scrollLeft, scrollTop} = current;
    current.replaceWith(next);
    if (focusedId) next.querySelector("#" + CSS.escape(focusedId))?.focus({preventScroll: true});
    else if (href) next.querySelector(`a[href=${CSS.escape(href)}]`)?.focus({preventScroll: true});
    next.scrollTo(scrollLeft, scrollTop);
  }

  const experimentTabs = document.getElementById("experiment-tabs");
  const charts = id === "experiment-status" && typeof drawExperimentCurves === "function";
  const draw = () => drawExperimentCurves(document.getElementById(id));
  function showExperimentTab() {
    const report = document.getElementById("report");
    report.hidden = location.hash !== "#report";
    if (!report.hidden && !report.hasAttribute("src")) report.src = report.dataset.src;
    for (const link of experimentTabs.querySelectorAll("a")) {
      if (link.hash === (report.hidden ? "#status" : "#report")) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  }

  function rememberSelection() {
    const pick = document.querySelector(".run-detail nav a[aria-current]");
    if (pick && location.search !== pick.search) history.replaceState(null, "", pick.search + location.hash);
  }

  if (experimentTabs) {
    showExperimentTab();
    rememberSelection();
    window.addEventListener("hashchange", () => {
      showExperimentTab();
      if (location.hash !== "#report") draw();
    });
    document.addEventListener("click", async event => {
      const button = event.target.closest(".command-bar button");
      if (!button) return;
      const command = button.querySelector("code");
      try {
        await navigator.clipboard.writeText(command.textContent);
        button.parentElement.querySelector(".lbl").textContent = "Copied";
      } catch {
        // Plain HTTP can lack clipboard access. Leave the command selected for copying.
        const range = document.createRange();
        range.selectNodeContents(command);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        button.parentElement.querySelector(".lbl").textContent = "Use your copy shortcut";
      }
    });
  }

  if (charts) {
    draw();
    let frame;
    window.addEventListener("resize", () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(draw);
    });
  }
  const output = document.getElementById("retained-output");
  if (output) output.scrollTop = output.scrollHeight;

  async function check() {
    let navigating = false;
    try {
      if (document.hidden) return;
      let url = path;
      if (id === "run-observation" && location.search) {
        const tab = new URLSearchParams(location.search).get("tab");
        if (tab) url += "&tab=" + encodeURIComponent(tab);
      }
      const mounted = experimentTabs && document.querySelector(".run-curves");
      if (experimentTabs) {
        const params = new URLSearchParams(location.search);
        if (mounted) params.set("curves", mounted.dataset.generation);
        url += "?" + params;
      }
      const response = await fetch(url);
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
        const written = document.getElementById("last-write");
        if (written) written.textContent = next.dataset.written;
        const collected = document.getElementById("console-collected");
        if (collected) {
          collected.textContent = next.dataset.collected;
          collected.hidden = !next.dataset.collected;
        }
      } else if (!railOnly) {
        const curves = next.querySelector(".run-curves");
        const keep = mounted && curves && curves.dataset.generation === mounted.dataset.generation;
        if (keep) curves.replaceWith(mounted);
        const oldOutput = current.querySelector("#retained-output");
        const follow = !oldOutput || oldOutput.scrollHeight - oldOutput.clientHeight - oldOutput.scrollTop < 2;
        const scroll = oldOutput?.scrollTop;
        replace(current, next);
        if (experimentTabs) rememberSelection();
        if (charts && !keep) draw();
        const tail = next.querySelector("#retained-output");
        if (tail) tail.scrollTop = follow ? tail.scrollHeight : scroll;
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
