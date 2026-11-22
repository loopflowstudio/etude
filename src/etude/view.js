let refreshing = false;
async function refreshReport() {
  if (refreshing) return;
  refreshing = true;
  const status = document.getElementById("refresh-status");
  if (status) status.textContent = "Refreshing…";
  try {
    const response = await fetch(location.pathname, {method: "POST"});
    if (!response.ok) throw new Error(await response.text());
    location.reload();
  } catch (error) {
    if (status) status.textContent = `Refresh failed: ${error.message}. Showing the last successful generation.`;
  } finally { refreshing = false; }
}
document.getElementById("refresh-report").onclick = refreshReport;
