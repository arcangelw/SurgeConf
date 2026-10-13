/** 策略分组页：地区/服务分组管理（由 groups.html 内联 JS 迁出；VIRT_LABELS 复用 rules-common.js） */

let allRegions = [];
let currentPolicies = [];

function getAvailableGroups() {
  const virtual = [
    { name: "DIRECT", label: "DIRECT", type: "system" },
    { name: "REJECT", label: "REJECT", type: "system" },
    { name: "全部节点", label: VIRT_LABELS["全部节点"], type: "virtual" },
    { name: "自动选择", label: VIRT_LABELS["自动选择"], type: "virtual" },
    { name: "代理", label: VIRT_LABELS["代理"], type: "virtual" },
    { name: "手动选择", label: VIRT_LABELS["手动选择"], type: "virtual" },
    { name: "大陆直连", label: VIRT_LABELS["大陆直连"], type: "virtual" },
  ];
  const autoRegions = allRegions.map(r => ({ name: r.name, label: I18N["group.auto_suffix"].replace("{0}", r.name), type: "auto" }));
  const manualRegions = allRegions.map(r => ({ name: `${r.name}手动`, label: I18N["group.manual_suffix"].replace("{0}", r.name), type: "manual" }));
  return { virtual, autoRegions, manualRegions };
}

let policySortable = null;

function renderPolicyTags() {
  const container = document.getElementById("policy-tags");
  if (currentPolicies.length === 0) {
    if (policySortable) { policySortable.destroy(); policySortable = null; }
    container.innerHTML = `<span style="color:var(--text2);font-size:12px;">${I18N["group.no_policy"]}</span>`;
    return;
  }
  if (policySortable) policySortable.destroy();
  container.innerHTML = currentPolicies.map((p) => {
    const isManual = p.endsWith("手动");
    const badgeClass = isManual ? 'badge-warning' : p === "DIRECT" ? 'badge-success' : 'badge-primary';
    const displayName = VIRT_LABELS[p] || translateName(p);
    return `<span class="badge ${badgeClass}"
      data-policy="${escapeHtml(p)}"
      style="cursor:grab;margin:2px;padding:4px 10px;font-size:12px;">
      ${escapeHtml(displayName)}
      <span class="policy-remove" style="margin-left:4px;opacity:0.6;cursor:pointer;">✕</span>
    </span>`;
  }).join("");
  if (typeof Sortable !== "undefined") {
    policySortable = new Sortable(container, {
      animation: 150,
      ghostClass: "sortable-ghost",
      onEnd(evt) {
        const [moved] = currentPolicies.splice(evt.oldIndex, 1);
        currentPolicies.splice(evt.newIndex, 0, moved);
        renderPolicyTags();
      },
    });
  }
}

// 政策标签移除按钮的事件委托（只绑定一次）
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("policy-tags")?.addEventListener("click", (e) => {
    if (e.target.classList.contains("policy-remove")) {
      const span = e.target.closest("[data-policy]");
      if (span) {
        const i = currentPolicies.indexOf(span.dataset.policy);
        if (i !== -1) removePolicy(i);
      }
    }
  });
});

function renderAvailableGroups() {
  const { virtual, autoRegions, manualRegions } = getAvailableGroups();
  const container = document.getElementById("available-groups");

  const renderSection = (title, groups) => {
    let html = `<div style="margin-bottom:8px;"><span style="font-size:11px;color:var(--text2);display:block;margin-bottom:4px;">${title}</span>`;
    for (const g of groups) {
      const added = currentPolicies.includes(g.name);
      const typeBadge = g.type === "manual" ? 'badge-warning' : g.type === "auto" ? 'badge-primary' : 'badge-success';
      html += `<span class="badge ${typeBadge}" style="cursor:${added ? 'default' : 'pointer'};margin:2px;padding:3px 8px;font-size:11px;${added ? 'opacity:0.4;' : ''}"
        onclick="${added ? '' : `addPolicy('${escapeHtml(g.name)}')`}">${escapeHtml(g.label)}</span>`;
    }
    html += '</div>';
    return html;
  };

  container.innerHTML =
    renderSection(I18N["group.section_basic"], virtual) +
    renderSection(I18N["group.section_auto"], autoRegions) +
    renderSection(I18N["group.section_manual"], manualRegions);
}

function addPolicy(name) {
  if (!currentPolicies.includes(name)) {
    currentPolicies.push(name);
    renderPolicyTags();
    renderAvailableGroups();
  }
}

function removePolicy(index) {
  currentPolicies.splice(index, 1);
  renderPolicyTags();
  renderAvailableGroups();
}

async function loadData() {
  const [regions, services] = await Promise.all([
    API.get("/api/groups/regions"),
    API.get("/api/groups/services"),
  ]);
  allRegions = regions;

  document.getElementById("region-list").innerHTML = regions.map(r => {
    const autoBadge = r.auto_enabled
      ? `<span class="badge badge-success" style="cursor:pointer;" onclick="toggleRegionMode(${r.id}, 'auto_enabled', false)">${I18N["btn.enable"]}</span>`
      : `<span class="badge badge-danger" style="cursor:pointer;" onclick="toggleRegionMode(${r.id}, 'auto_enabled', true)">${I18N["btn.disable"]}</span>`;
    const manualBadge = r.manual_enabled
      ? `<span class="badge badge-success" style="cursor:pointer;" onclick="toggleRegionMode(${r.id}, 'manual_enabled', false)">${I18N["btn.enable"]}</span>`
      : `<span class="badge badge-danger" style="cursor:pointer;" onclick="toggleRegionMode(${r.id}, 'manual_enabled', true)">${I18N["btn.enable"]}</span>`;
    const isActive = r.auto_enabled || r.manual_enabled;
    const statusBadge = isActive
      ? `<span class="badge badge-success">${I18N["btn.enable"]}</span>`
      : `<span class="badge badge-danger">${I18N["btn.disable"]}</span>`;
    return `
    <tr>
      <td>${r.sort_order}</td>
      <td><strong>${escapeHtml(translateName(r.name))}</strong></td>
      <td><code style="font-size:11px;color:var(--warning);">${escapeHtml(r.filter_regex || '-')}</code></td>
      <td>${autoBadge}</td>
      <td>${manualBadge}</td>
      <td>${statusBadge}</td>
      <td>
        <button class="btn btn-sm btn-outline" onclick='editRegion(${JSON.stringify(r)})'>${I18N["btn.edit"]}</button>
        <button class="btn btn-sm btn-danger" onclick="deleteRegion(${r.id}, '${escapeHtml(r.name)}')">${I18N["btn.delete"]}</button>
      </td>
    </tr>`;
  }).join("");
  compactActions();

  document.getElementById("service-list").innerHTML = services.map(s => `
    <tr>
      <td>${s.sort_order}</td>
      <td><strong>${escapeHtml(translateName(s.name))}</strong></td>
      <td><span class="badge badge-primary">${s.group_type}</span></td>
      <td>${(s.policies || []).map(p => {
        const isManual = p.endsWith("手动");
        const displayName = VIRT_LABELS[p] || translateName(p);
        return `<span class="badge ${isManual ? 'badge-warning' : 'badge-success'}" style="margin:2px;">${escapeHtml(displayName)}</span>`;
      }).join(" ")}</td>
      <td>
        <button class="btn btn-sm btn-outline" onclick='editService(${JSON.stringify(s)})'>${I18N["btn.edit"]}</button>
        <button class="btn btn-sm btn-danger" onclick="deleteService(${s.id}, '${escapeHtml(s.name)}')">${I18N["btn.delete"]}</button>
      </td>
    </tr>
  `).join("");
  compactActions();
}

function showAddRegion() {
  document.getElementById("region-modal-title").textContent = I18N["group.add_region"];
  document.getElementById("region-edit-id").value = "";
  document.getElementById("region-name").value = "";
  document.getElementById("region-regex").value = "";
  document.getElementById("region-icon").value = "";
  document.getElementById("region-auto-enabled").value = "true";
  document.getElementById("region-manual-enabled").value = "true";
  document.getElementById("region-order").value = 10;
  showModal("region-modal");
}

function editRegion(r) {
  document.getElementById("region-modal-title").textContent = I18N["btn.edit"];
  document.getElementById("region-edit-id").value = r.id;
  document.getElementById("region-name").value = r.name;
  document.getElementById("region-regex").value = r.filter_regex || "";
  document.getElementById("region-icon").value = r.icon_url || "";
  document.getElementById("region-auto-enabled").value = String(r.auto_enabled);
  document.getElementById("region-manual-enabled").value = String(r.manual_enabled);
  document.getElementById("region-order").value = r.sort_order;
  showModal("region-modal");
}

async function saveRegion() {
  const id = document.getElementById("region-edit-id").value;
  const nameEl = document.getElementById("region-name");
  if (!nameEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(nameEl); return;
  }
  const data = {
    name: nameEl.value.trim(),
    filter_regex: document.getElementById("region-regex").value || null,
    icon_url: document.getElementById("region-icon").value || null,
    auto_enabled: document.getElementById("region-auto-enabled").value === "true",
    manual_enabled: document.getElementById("region-manual-enabled").value === "true",
    sort_order: parseInt(document.getElementById("region-order").value) || 0,
  };
  try {
    if (id) {
      await API.put(`/api/groups/regions/${id}`, data);
      toast(I18N["msg.updated"]);
    } else {
      await API.post("/api/groups/regions", data);
      toast(I18N["msg.added"]);
    }
    hideModal("region-modal");
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

async function toggleRegionMode(id, field, value) {
  try {
    await API.put(`/api/groups/regions/${id}`, { [field]: value });
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteRegion(id, name) {
  if (!(await confirmDialog(I18N["group.confirm_delete_region"].replace("{0}", name), { danger: true }))) return;
  try {
    await API.del(`/api/groups/regions/${id}`);
    toast(I18N["msg.deleted"]);
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

function showAddService() {
  document.getElementById("service-modal-title").textContent = I18N["group.add_service"];
  document.getElementById("service-edit-id").value = "";
  document.getElementById("service-name").value = "";
  document.getElementById("service-type").value = "select";
  document.getElementById("service-icon").value = "";
  document.getElementById("service-order").value = 10;
  currentPolicies = [];
  renderPolicyTags();
  renderAvailableGroups();
  showModal("service-modal");
}

function editService(s) {
  document.getElementById("service-modal-title").textContent = I18N["btn.edit"];
  document.getElementById("service-edit-id").value = s.id;
  document.getElementById("service-name").value = s.name;
  document.getElementById("service-type").value = s.group_type;
  document.getElementById("service-icon").value = s.icon_url || "";
  document.getElementById("service-order").value = s.sort_order;
  currentPolicies = [...(s.policies || [])];
  renderPolicyTags();
  renderAvailableGroups();
  showModal("service-modal");
}

async function saveService() {
  const id = document.getElementById("service-edit-id").value;
  const nameEl = document.getElementById("service-name");
  if (!nameEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(nameEl); return;
  }
  const data = {
    name: nameEl.value.trim(),
    group_type: document.getElementById("service-type").value,
    policies: currentPolicies,
    icon_url: document.getElementById("service-icon").value || null,
    sort_order: parseInt(document.getElementById("service-order").value) || 0,
  };
  try {
    if (id) {
      await API.put(`/api/groups/services/${id}`, data);
      toast(I18N["msg.updated"]);
    } else {
      await API.post("/api/groups/services", data);
      toast(I18N["msg.added"]);
    }
    hideModal("service-modal");
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteService(id, name) {
  if (!(await confirmDialog(I18N["group.confirm_delete_service"].replace("{0}", name), { danger: true }))) return;
  try {
    await API.del(`/api/groups/services/${id}`);
    toast(I18N["msg.deleted"]);
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

loadData();
