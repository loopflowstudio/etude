const refresh = document.getElementById("refresh-report");
refresh.onclick = async () => {
  refresh.disabled = true;
  const status = document.getElementById("refresh-status");
  status.textContent = "Refreshing…";
  try {
    const response = await fetch(location.href, {method: "POST"});
    if (!response.ok) throw new Error(await response.text());
    location.reload();
  } catch (error) {
    status.textContent = `Refresh failed: ${error.message}. Showing the last successful generation.`;
    refresh.disabled = false;
  }
};
