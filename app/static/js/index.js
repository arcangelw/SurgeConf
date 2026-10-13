/** 仪表盘页（由 index.html 内联 JS 迁出） */

async function refreshStats() {
  try {
    const [subs, nodesByRegion, regions, services, rules, profiles] = await Promise.all([
      API.get("/api/subscriptions"),
      API.get("/api/nodes/by-region"),
      API.get("/api/groups/regions"),
      API.get("/api/groups/services"),
      API.get("/api/configs/rules"),
      API.get("/api/configs/profiles"),
    ]);

    document.getElementById("stat-subs").textContent = subs.length;
    document.getElementById("stat-regions").textContent = regions.length;
    document.getElementById("stat-services").textContent = services.length;
    document.getElementById("stat-rules").textContent = rules.length;
    document.getElementById("stat-profiles").textContent = profiles.length;

    const groups = nodesByRegion.groups || {};
    let totalNodes = 0;
    const chartEl = document.getElementById("region-chart");
    chartEl.innerHTML = "";

    for (const [region, nodes] of Object.entries(groups)) {
      totalNodes += nodes.length;
      const bar = document.createElement("div");
      bar.style.cssText = "background:var(--surface2);border-radius:6px;padding:10px;min-width:100px;text-align:center;";
      bar.innerHTML = `<div style="font-size:20px;font-weight:700;">${nodes.length}</div><div style="font-size:11px;color:var(--text2);margin-top:4px;">${escapeHtml(region)}</div>`;
      chartEl.appendChild(bar);
    }

    totalNodes += (nodesByRegion.unassigned || []).length;
    document.getElementById("stat-nodes").textContent = totalNodes;
  } catch (e) {
    toast(e.message, "error");
  }
}

async function syncAll(btn) {
  await withLoading(btn, async () => {
    try {
      toast(I18N["dashboard.syncing"]);
      const result = await API.post("/api/subscriptions/sync-all");
      toast(I18N["dashboard.sync_done"].replace("{0}", result.results.length));
      refreshStats();
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

async function autoAssign(btn) {
  await withLoading(btn, async () => {
    try {
      toast(I18N["dashboard.assigning"]);
      const result = await API.post("/api/nodes/auto-assign");
      toast(I18N["dashboard.assigned"].replace("{0}", result.assigned));
      refreshStats();
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

refreshStats();
