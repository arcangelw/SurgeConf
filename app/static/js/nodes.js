/** 节点分组页：筛选/批量操作/钉选服务弹窗（由 nodes.html 内联 JS 迁出） */

let selectedNodes = new Set();
let allServices = [];
let allRegions = [];

async function loadFilters() {
  const [regions, services, subs] = await Promise.all([
    API.get("/api/groups/regions"),
    API.get("/api/groups/services"),
    API.get("/api/subscriptions"),
  ]);
  allServices = services;
  allRegions = regions;

  const rSel = document.getElementById("filter-region");
  rSel.innerHTML = `<option value="">${I18N["node.all_regions"]}</option>` +
    regions.map(r => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.display_name || r.name)}</option>`).join("") +
    `<option value="__unassigned__">${I18N["node.no_region"]}</option>`;

  const sSel = document.getElementById("filter-sub");
  sSel.innerHTML = `<option value="">${I18N["node.all_subs"]}</option>` +
    subs.map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("");

  const bSel = document.getElementById("batch-target");
  bSel.innerHTML = regions.map(r => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)}</option>`).join("") +
    `<option disabled>${I18N["node.pin_service_sep"]}</option>` +
    services.map(s => `<option value="pin:${escapeHtml(s.name)}">${I18N["node.pin_to"].replace("{0}", escapeHtml(s.name))}</option>`).join("");
}

async function loadData() {
  const region = document.getElementById("filter-region").value;
  const subId = document.getElementById("filter-sub").value;
  const pinned = document.getElementById("filter-pinned").value;

  let url = "/api/nodes?";
  if (region) url += region === "__unassigned__" ? "unassigned=true&" : `region=${encodeURIComponent(region)}&`;
  if (subId) url += `subscription_id=${subId}&`;

  try {
    showLoading();
    const nodes = await API.get(url);
    let filtered = nodes;
    if (pinned === "__none__") filtered = nodes.filter(n => !n.pinned_services || n.pinned_services.length === 0);
    if (pinned === "__any__") filtered = nodes.filter(n => n.pinned_services && n.pinned_services.length > 0);

    document.getElementById("node-total").textContent = I18N["node.total_count"].replace("{0}", filtered.length);
    const tbody = document.getElementById("node-table");
    const empty = document.getElementById("empty-state");

    if (filtered.length === 0) {
      tbody.innerHTML = "";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";

    tbody.innerHTML = filtered.map(n => {
      const regionBadge = n.region_manual
        ? `<span class="badge badge-warning">${I18N["node.manual_label"].replace("{0}", escapeHtml(n.region_manual))}</span>`
        : n.region_auto
          ? `<span class="badge badge-primary">${I18N["node.auto_label"].replace("{0}", escapeHtml(n.region_auto))}</span>`
          : '<span style="color:var(--text2)">-</span>';
      const pinnedBadges = (n.pinned_services || []).map(s =>
        `<span class="badge badge-success" style="margin:1px;">${escapeHtml(s)}</span>`
      ).join("") || '<span style="color:var(--text2)">-</span>';
      const subColor = n.subscription_color || '#6c5ce7';
      const subTag = n.subscription_tag || n.subscription_name || '';

      return `<tr>
        <td><input type="checkbox" class="node-check" data-id="${n.id}" ${selectedNodes.has(n.id) ? 'checked' : ''}></td>
        <td>
          <div style="display:flex;align-items:center;gap:6px;">
            <span style="width:6px;height:6px;border-radius:50%;background:${subColor};flex-shrink:0;"></span>
            <a href="javascript:void(0)" onclick="showDetail(${n.id})" style="color:var(--primary);text-decoration:none;">${escapeHtml(n.name)}</a>
          </div>
        </td>
        <td><span style="font-size:11px;color:${subColor};">${escapeHtml(subTag)}</span></td>
        <td><span class="badge badge-primary" style="font-size:10px;">${n.node_type}</span></td>
        <td>${regionBadge}</td>
        <td>${pinnedBadges}</td>
        <td>
          <button class="btn btn-sm btn-outline" onclick="pinToService(${n.id})">${I18N["node.pin_btn"]}</button>
        </td>
      </tr>`;
    }).join("");
    compactActions();
    hideLoading();
  } catch (e) { toast(e.message, "error"); hideLoading(); }
}

async function showDetail(nodeId) {
  try {
    const n = await API.get(`/api/nodes/${nodeId}`);
    const pinned = (n.pinned_services || []).join(", ") || "-";
    document.getElementById("detail-content").innerHTML = `
      <table style="width:100%;">
        <tr><td style="color:var(--text2);width:100px;">${I18N["node.detail_name"]}</td><td><strong>${escapeHtml(n.name)}</strong></td></tr>
        <tr><td style="color:var(--text2);">${I18N["node.detail_type"]}</td><td>${n.node_type}</td></tr>
        <tr><td style="color:var(--text2);">${I18N["node.detail_server"]}</td><td>${escapeHtml(n.server)}:${n.port}</td></tr>
        <tr><td style="color:var(--text2);">${I18N["node.detail_sub"]}</td><td>${escapeHtml(n.subscription_name || '-')}</td></tr>
        <tr><td style="color:var(--text2);">${I18N["node.detail_auto_region"]}</td><td>${n.region_auto || '-'}</td></tr>
        <tr><td style="color:var(--text2);">${I18N["node.detail_manual_region"]}</td><td>${n.region_manual || '-'}</td></tr>
        <tr><td style="color:var(--text2);">${I18N["node.detail_pinned"]}</td><td>${escapeHtml(pinned)}</td></tr>
      </table>
    `;
    showModal("detail-modal");
  } catch (e) { toast(e.message, "error"); }
}

function toggleAll(el) {
  const checked = el.checked;
  document.querySelectorAll(".node-check").forEach(cb => {
    const id = parseInt(cb.dataset.id);
    cb.checked = checked;
    if (checked) selectedNodes.add(id);
    else selectedNodes.delete(id);
  });
  updateBatchBar();
}

function updateBatchBar() {
  const bar = document.getElementById("batch-bar");
  if (selectedNodes.size > 0) {
    bar.style.display = "flex";
    document.getElementById("batch-count").textContent = I18N["node.selected_count"].replace("{0}", selectedNodes.size);
  } else {
    bar.style.display = "none";
  }
}

function clearSelection() {
  selectedNodes.clear();
  document.getElementById("select-all").checked = false;
  document.querySelectorAll(".node-check").forEach(cb => cb.checked = false);
  updateBatchBar();
}

async function batchMove(btn) {
  const target = document.getElementById("batch-target").value;
  if (!target || selectedNodes.size === 0 || target.startsWith("pin:")) return;
  await withLoading(btn, async () => {
    try {
      await API.put("/api/nodes/batch/region", { node_ids: Array.from(selectedNodes), region_manual: target });
      toast(I18N["node.moved"].replace("{0}", selectedNodes.size).replace("{1}", target));
      clearSelection(); loadData();
    } catch (e) { toast(e.message, "error"); }
  });
}

// ===== 钉选服务弹窗（多选 checkbox，替代原生 prompt） =====

/** 打开钉选弹窗。mode: "pin" | "unpin"；nodeIds: 目标节点集合；onDone: 完成回调 */
function openPinModal(mode, nodeIds, onDone) {
  const list = document.getElementById("pin-service-list");
  const rows = allServices.map(s => `
    <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;cursor:pointer;">
      <input type="checkbox" class="pin-svc-check" value="${escapeHtml(s.name)}">
      <span>${escapeHtml(translateName(s.name))}</span>
    </label>`).join("");
  const unpinAll = mode === "unpin"
    ? `<label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-top:1px solid var(--border);margin-top:6px;cursor:pointer;color:var(--danger);">
        <input type="checkbox" id="pin-unpin-all"> <span>${I18N["node.unpin_all"]}</span></label>`
    : "";
  list.innerHTML = rows + unpinAll;
  document.getElementById("pin-modal-title").textContent =
    mode === "pin" ? I18N["node.pin_modal_title"] : I18N["node.unpin_modal_title"];
  document.getElementById("pin-modal-confirm").onclick = async () => {
    const btn = document.getElementById("pin-modal-confirm");
    const unpinAllChecked = document.getElementById("pin-unpin-all")?.checked;
    const services = Array.from(list.querySelectorAll(".pin-svc-check:checked")).map(cb => cb.value);
    if (!unpinAllChecked && services.length === 0) {
      toast(I18N["node.no_service_selected"], "error");
      return;
    }
    await withLoading(btn, async () => {
      try {
        if (unpinAllChecked) {
          // 清空：逐个服务批量移除（服务端无「清空」端点，等价于对所有已绑定服务 unpin）
          const every = allServices.map(s => s.name);
          for (const svc of every) {
            await API.put("/api/nodes/batch/pin", { node_ids: Array.from(nodeIds), service_name: svc, pinned: false });
          }
        } else {
          for (const svc of services) {
            await API.put("/api/nodes/batch/pin", { node_ids: Array.from(nodeIds), service_name: svc, pinned: mode === "pin" });
          }
        }
        toast(mode === "pin"
          ? I18N["node.pinned_to"].replace("{0}", services.join(", "))
          : I18N["node.unpinned"].replace("{0}", nodeIds.size));
        hideModal("pin-modal");
        onDone?.();
      } catch (e) { toast(e.message, "error"); }
    });
  };
  showModal("pin-modal");
}

async function batchPin(btn) {
  if (selectedNodes.size === 0) return;
  await withLoading(btn, () => openPinModal("pin", new Set(selectedNodes), () => { clearSelection(); loadData(); }));
}

async function batchUnpin(btn) {
  if (selectedNodes.size === 0) return;
  await withLoading(btn, () => openPinModal("unpin", new Set(selectedNodes), () => { clearSelection(); loadData(); }));
}

async function pinToService(nodeId) {
  openPinModal("pin", new Set([nodeId]), () => loadData());
}

async function autoAssign(btn) {
  await withLoading(btn, async () => {
    try {
      toast(I18N["node.assigning"]);
      const result = await API.post("/api/nodes/auto-assign");
      toast(I18N["node.assigned"].replace("{0}", result.assigned));
      loadData();
    } catch (e) { toast(e.message, "error"); }
  });
}

document.addEventListener("change", (e) => {
  if (e.target.classList?.contains("node-check")) {
    const id = parseInt(e.target.dataset.id);
    if (e.target.checked) selectedNodes.add(id);
    else selectedNodes.delete(id);
    updateBatchBar();
  }
});

async function init() {
  await loadFilters();
  await loadData();
}
init();
