/** 策略页：双栏树形（平移自 subscriptions/nodes/groups 三页；VIRT_LABELS 复用 rules-common.js）
 *  左栏：全部节点 / 订阅源 / 地区组（可拖拽排序）/ 服务组（可拖拽排序）
 *  右栏：按 selection 渲染——all=节点全表，sub=源信息+节点表，region=组信息+成员表，service=组信息+钉选成员表 */

// ===== 状态 =====

let selection = "all"; // "all" | "sub:{id}" | "region:{id}" | "service:{id}"
let allSubs = [];
let allRegionGroups = [];
let allServiceGroups = [];
let byRegion = { groups: {}, unassigned: [] };
let selectedNodes = new Set();
let currentPolicies = []; // 服务组弹窗策略链（groups.js 平移）

// ===== 核心数据加载 =====

async function loadCore() {
  const [subs, br, regions, services] = await Promise.all([
    API.get("/api/subscriptions"),
    API.get("/api/nodes/by-region"),
    API.get("/api/groups/regions"),
    API.get("/api/groups/services"),
  ]);
  allSubs = subs;
  byRegion = br;
  allRegionGroups = regions;
  allServiceGroups = services;
}

async function reloadGroups() {
  [allRegionGroups, allServiceGroups] = await Promise.all([
    API.get("/api/groups/regions"),
    API.get("/api/groups/services"),
  ]);
}

function allNodesFlat() {
  return [...Object.values(byRegion.groups).flat(), ...(byRegion.unassigned || [])];
}

function regionMemberCount(name) {
  return (byRegion.groups[name] || []).length;
}

function serviceMemberCount(name) {
  return allNodesFlat().filter(n => (n.pinned_services || []).includes(name)).length;
}

function totalNodeCount() {
  return allNodesFlat().length;
}

/** 写操作后统一刷新：核心数据 + 左栏 + 右栏 */
async function refreshAll() {
  await loadCore();
  renderSidebar();
  renderMain();
}

// ===== selection 与 hash 同步 =====

function parseHash() {
  const m = location.hash.match(/^#(sub|region|service)\/(\d+)$/);
  return m ? `${m[1]}:${m[2]}` : "all";
}

function setSelection(sel) {
  if (sel === selection) return;
  selection = sel;
  selectedNodes.clear();
  updateBatchBar();
  const hash = sel === "all" ? "" : "#" + sel.replace(":", "/");
  history.replaceState(null, "", hash || location.pathname + location.search);
  renderSidebar();
  renderMain();
}

window.addEventListener("hashchange", () => {
  const sel = parseHash();
  if (sel !== selection) {
    selection = sel;
    selectedNodes.clear();
    renderSidebar();
    renderMain();
  }
});

// ===== 左栏渲染 =====

function renderSidebar() {
  const nav = document.getElementById("px-nav");

  const item = (sel, label, opts = {}) => {
    const active = selection === sel ? " active" : "";
    return `<div class="px-item${active}" data-sel="${sel}" data-id="${opts.id ?? ""}" onclick="setSelection('${sel}')">
      ${opts.dot ? `<span class="px-dot" style="background:${opts.dot};"></span>` : ""}
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(label)}</span>
      ${opts.count != null ? `<span class="px-count">${opts.count}</span>` : ""}
      ${opts.grip ? `<span class="px-grip" title="拖拽排序">⠿</span>` : ""}
    </div>`;
  };

  const section = (title, itemsHtml, addBtn) =>
    `<div class="px-section">
      <div class="px-section-title">${escapeHtml(title)}</div>
      ${itemsHtml}
      ${addBtn || ""}
    </div>`;

  nav.innerHTML =
    item("all", I18N["proxies.all_nodes"], { count: totalNodeCount() }) +
    section(I18N["proxies.section_subs"],
      allSubs.map(s => item(`sub:${s.id}`, translateName(s.name), {
        id: s.id,
        dot: s.enabled ? (s.color || "#6c5ce7") : "var(--text2)",
      })).join(""),
      `<button class="px-add" onclick="showAddModal()">+ ${I18N["btn.add"]}</button>`) +
    `<div id="px-region-list">` +
    section(I18N["proxies.section_regions"],
      allRegionGroups.map(r => item(`region:${r.id}`, translateName(r.name), {
        id: r.id, grip: true, count: regionMemberCount(r.name),
      })).join(""),
      `<button class="px-add" onclick="showAddRegion()">+ ${I18N["proxies.new_group"]}</button>`) +
    `</div>` +
    `<div id="px-service-list">` +
    section(I18N["proxies.section_services"],
      allServiceGroups.map(s => item(`service:${s.id}`, translateName(s.name), {
        id: s.id, grip: true, count: serviceMemberCount(s.name),
      })).join(""),
      `<button class="px-add" onclick="showAddService()">+ ${I18N["proxies.new_group"]}</button>`) +
    `</div>`;

  initSidebarSort();
}

function initSidebarSort() {
  if (typeof Sortable === "undefined") return;
  for (const listId of ["px-region-list", "px-service-list"]) {
    const el = document.getElementById(listId);
    if (!el) continue;
    new Sortable(el, {
      animation: 150,
      ghostClass: "sortable-ghost",
      handle: ".px-item",
      onEnd: () => saveSidebarOrder(el, listId),
    });
  }
}

async function saveSidebarOrder(el, listId) {
  const base = listId === "px-region-list" ? "/api/groups/regions" : "/api/groups/services";
  // el 内可能有多节，统一按 DOM 顺序收集所有条目 id
  const ids = [...el.querySelectorAll(".px-item")]
    .map(n => parseInt(n.dataset.id))
    .filter(Boolean);
  try {
    for (let i = 0; i < ids.length; i++) {
      await API.put(`${base}/${ids[i]}`, { sort_order: i });
    }
    toast(I18N["proxies.order_saved"]);
    await reloadGroups();
    renderSidebar();
  } catch (e) { toast(e.message, "error"); }
}

// ===== 右栏渲染（按 selection 分派） =====

function renderMain() {
  const main = document.getElementById("px-main");
  main.innerHTML = "";
  const [kind, id] = selection.split(":");
  if (kind === "sub") renderSubDetail(parseInt(id));
  else if (kind === "region") renderRegionDetail(parseInt(id));
  else if (kind === "service") renderServiceDetail(parseInt(id));
  else renderAllNodes();
}

// ----- all：全部节点（nodes 页全量能力平移） -----

function renderAllNodes() {
  const main = document.getElementById("px-main");
  main.innerHTML = `
    <div class="card" style="margin-bottom:16px;padding:12px 20px;">
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
        <input type="text" id="node-search" placeholder="${I18N["proxies.search_placeholder"]}"
          oninput="loadNodes()" style="width:160px;">
        <select id="filter-region" onchange="loadNodes()" style="width:130px;"></select>
        <select id="filter-sub" onchange="loadNodes()" style="width:130px;"></select>
        <select id="filter-pinned" onchange="loadNodes()" style="width:130px;">
          <option value="">${I18N["node.pin_status"]}</option>
          <option value="__none__">${I18N["node.not_pinned"]}</option>
          <option value="__any__">${I18N["node.has_pinned"]}</option>
        </select>
        <span id="node-total" style="font-size:12px;color:var(--text2);margin-left:auto;"></span>
      </div>
    </div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width:30px;"><input type="checkbox" id="select-all" onchange="toggleAll(this)"></th>
              <th>${I18N["node.col_name"]}</th>
              <th>${I18N["node.col_sub"]}</th>
              <th>${I18N["node.col_type"]}</th>
              <th>${I18N["node.col_region"]}</th>
              <th>${I18N["node.col_pinned"]} <span class="help-tip" data-tip="${I18N["node.col_pinned_tip"]}">?</span></th>
              <th>${I18N["node.col_action"]}</th>
            </tr>
          </thead>
          <tbody id="node-table"></tbody>
        </table>
      </div>
      <div id="empty-state" class="empty" style="display:none;">
        <div class="icon">🌐</div>
        <p>${I18N["node.empty"]}</p>
      </div>
    </div>`;
  loadFilters();
  loadNodes();
}

async function loadFilters() {
  const rSel = document.getElementById("filter-region");
  if (rSel) {
    rSel.innerHTML = `<option value="">${I18N["node.all_regions"]}</option>` +
      allRegionGroups.map(r => `<option value="${escapeHtml(r.name)}">${escapeHtml(translateName(r.name))}</option>`).join("") +
      `<option value="__unassigned__">${I18N["node.no_region"]}</option>`;
  }
  const sSel = document.getElementById("filter-sub");
  if (sSel) {
    sSel.innerHTML = `<option value="">${I18N["node.all_subs"]}</option>` +
      allSubs.map(s => `<option value="${s.id}">${escapeHtml(translateName(s.name))}</option>`).join("");
  }
  const bSel = document.getElementById("batch-target");
  bSel.innerHTML = allRegionGroups.map(r => `<option value="${escapeHtml(r.name)}">${escapeHtml(translateName(r.name))}</option>`).join("") +
    `<option disabled>${I18N["node.pin_service_sep"]}</option>` +
    allServiceGroups.map(s => `<option value="pin:${escapeHtml(s.name)}">${I18N["node.pin_to"].replace("{0}", escapeHtml(translateName(s.name)))}</option>`).join("");
}

async function loadNodes() {
  const region = document.getElementById("filter-region")?.value || "";
  const subId = document.getElementById("filter-sub")?.value || "";
  const pinned = document.getElementById("filter-pinned")?.value || "";
  const search = (document.getElementById("node-search")?.value || "").trim().toLowerCase();

  let url = "/api/nodes?";
  if (region) url += region === "__unassigned__" ? "unassigned=true&" : `region=${encodeURIComponent(region)}&`;
  if (subId) url += `subscription_id=${subId}&`;

  try {
    showLoading();
    let nodes = await API.get(url);
    if (pinned === "__none__") nodes = nodes.filter(n => !n.pinned_services || n.pinned_services.length === 0);
    if (pinned === "__any__") nodes = nodes.filter(n => n.pinned_services && n.pinned_services.length > 0);
    if (search) nodes = nodes.filter(n => n.name.toLowerCase().includes(search));

    const filtered = nodes;
    const totalEl = document.getElementById("node-total");
    if (totalEl) totalEl.textContent = I18N["node.total_count"].replace("{0}", filtered.length);
    const tbody = document.getElementById("node-table");
    const empty = document.getElementById("empty-state");
    if (!tbody) { hideLoading(); return; }

    if (filtered.length === 0) {
      tbody.innerHTML = "";
      if (empty) empty.style.display = "block";
      hideLoading();
      return;
    }
    if (empty) empty.style.display = "none";
    tbody.innerHTML = filtered.map(nodeRowFull).join("");
    compactActions();
    hideLoading();
  } catch (e) { toast(e.message, "error"); hideLoading(); }
}

// ----- sub：订阅源信息卡 + 该源节点表 -----

async function renderSubDetail(subId) {
  const sub = allSubs.find(s => s.id === subId);
  const main = document.getElementById("px-main");
  if (!sub) {
    main.innerHTML = `<div class="card"><div class="empty"><p>${I18N["sub.empty"]}</p></div></div>`;
    return;
  }
  main.innerHTML = `
    <div class="card" style="margin-bottom:16px;">
      <div class="card-header">
        <div>
          <h3 style="display:flex;align-items:center;gap:8px;">
            <span style="width:10px;height:10px;border-radius:50%;background:${sub.color};flex-shrink:0;"></span>
            ${escapeHtml(translateName(sub.name))}
            <span class="badge ${sub.enabled ? 'badge-success' : 'badge-danger'}">${sub.enabled ? I18N["btn.enable"] : I18N["btn.disable"]}</span>
          </h3>
          <p style="font-size:12px;color:var(--text2);margin-top:4px;word-break:break-all;">${escapeHtml(sub.url)}</p>
          <p style="font-size:12px;color:var(--text2);margin-top:2px;">
            ${sub.tag ? `<span class="badge" style="background:${sub.color}22;color:${sub.color};border:1px solid ${sub.color}44;">${escapeHtml(sub.tag)}</span> ` : ""}
            ${I18N["sub.last_update"]}: ${escapeHtml(sub.last_update || I18N["sub.not_synced"])}
          </p>
        </div>
        <div class="px-info-actions">
          <button class="btn btn-sm btn-success" onclick="syncOne(${sub.id}, this)">${I18N["btn.sync"]}</button>
          <button class="btn btn-sm btn-outline" onclick='editSub(${JSON.stringify(sub)})'>${I18N["btn.edit"]}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteSub(${sub.id}, '${escapeHtml(sub.name)}')">${I18N["btn.delete"]}</button>
        </div>
      </div>
    </div>
    <div class="card">
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px;">
        <input type="text" id="node-search" placeholder="${I18N["proxies.search_placeholder"]}"
          oninput="loadSubNodes(${subId})" style="width:160px;">
        <span id="node-total" style="font-size:12px;color:var(--text2);margin-left:auto;"></span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width:30px;"><input type="checkbox" id="select-all" onchange="toggleAll(this)"></th>
              <th>${I18N["node.col_name"]}</th>
              <th>${I18N["node.col_type"]}</th>
              <th>${I18N["node.col_region"]}</th>
              <th>${I18N["node.col_pinned"]} <span class="help-tip" data-tip="${I18N["node.col_pinned_tip"]}">?</span></th>
              <th>${I18N["node.col_action"]}</th>
            </tr>
          </thead>
          <tbody id="node-table"></tbody>
        </table>
      </div>
      <div id="empty-state" class="empty" style="display:none;">
        <div class="icon">🌐</div>
        <p>${I18N["node.empty"]}</p>
      </div>
    </div>`;
  loadSubNodes(subId);
}

async function loadSubNodes(subId) {
  const search = (document.getElementById("node-search")?.value || "").trim().toLowerCase();
  try {
    showLoading();
    let nodes = await API.get(`/api/nodes?subscription_id=${subId}`);
    if (search) nodes = nodes.filter(n => n.name.toLowerCase().includes(search));
    const totalEl = document.getElementById("node-total");
    if (totalEl) totalEl.textContent = I18N["node.total_count"].replace("{0}", nodes.length);
    const tbody = document.getElementById("node-table");
    const empty = document.getElementById("empty-state");
    if (nodes.length === 0) {
      tbody.innerHTML = "";
      empty.style.display = "block";
      hideLoading();
      return;
    }
    empty.style.display = "none";
    tbody.innerHTML = nodes.map(nodeRowFull).join("");
    compactActions();
    hideLoading();
  } catch (e) { toast(e.message, "error"); hideLoading(); }
}

// ----- region：组信息卡 + 成员表 -----

async function renderRegionDetail(groupId) {
  const g = allRegionGroups.find(r => r.id === groupId);
  const main = document.getElementById("px-main");
  if (!g) {
    main.innerHTML = `<div class="card"><div class="empty"><p>${I18N["group.region_hint"]}</p></div></div>`;
    return;
  }
  const isActive = g.auto_enabled || g.manual_enabled;
  main.innerHTML = `
    <div class="card" style="margin-bottom:16px;">
      <div class="card-header">
        <div>
          <h3 style="display:flex;align-items:center;gap:8px;">
            ${escapeHtml(translateName(g.name))}
            <span class="badge ${isActive ? 'badge-success' : 'badge-danger'}">${isActive ? I18N["btn.enable"] : I18N["btn.disable"]}</span>
          </h3>
          <p style="font-size:12px;color:var(--text2);margin-top:4px;">
            <code style="color:var(--warning);">${escapeHtml(g.filter_regex || '-')}</code>
          </p>
        </div>
        <div class="px-info-actions">
          <span class="badge ${g.auto_enabled ? 'badge-success' : 'badge-danger'}" style="cursor:pointer;"
            onclick="toggleRegionMode(${g.id}, 'auto_enabled', ${!g.auto_enabled})">${I18N["group.auto_label"]}</span>
          <span class="badge ${g.manual_enabled ? 'badge-success' : 'badge-danger'}" style="cursor:pointer;"
            onclick="toggleRegionMode(${g.id}, 'manual_enabled', ${!g.manual_enabled})">${I18N["group.manual_label"]}</span>
          <button class="btn btn-sm btn-outline" onclick='editRegion(${JSON.stringify(g)})'>${I18N["btn.edit"]}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteRegion(${g.id}, '${escapeHtml(g.name)}')">${I18N["btn.delete"]}</button>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <h3>${I18N["proxies.region_members"]} <span id="member-count" class="badge badge-primary" style="margin-left:4px;">0</span></h3>
        <button class="btn btn-primary btn-sm" onclick="openRegionAddNodes(${g.id})">${I18N["proxies.add_nodes"]}</button>
      </div>
      <p style="font-size:12px;color:var(--text2);margin-bottom:8px;">${I18N["group.region_hint"]}</p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>${I18N["node.col_name"]}</th>
              <th>${I18N["node.col_sub"]}</th>
              <th>${I18N["node.col_region"]}</th>
              <th>${I18N["node.col_action"]}</th>
            </tr>
          </thead>
          <tbody id="member-table"></tbody>
        </table>
      </div>
      <div id="empty-state" class="empty" style="display:none;">
        <div class="icon">🌐</div>
        <p>${I18N["node.empty"]}</p>
      </div>
    </div>`;
  loadRegionMembers(g.name);
}

async function loadRegionMembers(name) {
  try {
    showLoading();
    const nodes = await API.get(`/api/nodes?region=${encodeURIComponent(name)}`);
    const countEl = document.getElementById("member-count");
    if (countEl) countEl.textContent = nodes.length;
    const tbody = document.getElementById("member-table");
    const empty = document.getElementById("empty-state");
    if (nodes.length === 0) {
      tbody.innerHTML = "";
      empty.style.display = "block";
      hideLoading();
      return;
    }
    empty.style.display = "none";
    tbody.innerHTML = nodes.map(n => nodeRowLite(n, `
      <button class="btn btn-sm btn-danger" onclick="removeFromRegion(${n.id}, this)">${I18N["proxies.remove"]}</button>
    `)).join("");
    compactActions();
    hideLoading();
  } catch (e) { toast(e.message, "error"); hideLoading(); }
}

async function removeFromRegion(nodeId, btn) {
  await withLoading(btn, async () => {
    try {
      await API.put(`/api/nodes/${nodeId}`, { region_manual: null });
      toast(I18N["msg.updated"]);
      await refreshAll();
    } catch (e) { toast(e.message, "error"); }
  });
}

function openRegionAddNodes(groupId) {
  const g = allRegionGroups.find(r => r.id === groupId);
  if (!g) return;
  const members = new Set((byRegion.groups[g.name] || []).map(n => n.id));
  openNodePicker(I18N["proxies.add_nodes"], members, async ids => {
    await API.put("/api/nodes/batch/region", { node_ids: ids, region_manual: g.name });
    toast(I18N["node.moved"].replace("{0}", ids.length).replace("{1}", translateName(g.name)));
    await refreshAll();
  });
}

// ----- service：组信息卡 + 钉选成员表 -----

async function renderServiceDetail(groupId) {
  const g = allServiceGroups.find(s => s.id === groupId);
  const main = document.getElementById("px-main");
  if (!g) {
    main.innerHTML = `<div class="card"><div class="empty"><p>${I18N["group.service_title"]}</p></div></div>`;
    return;
  }
  const policyTags = (g.policies || []).map(p => {
    const isManual = p.endsWith("手动");
    const displayName = VIRT_LABELS[p] || translateName(p);
    return `<span class="badge ${isManual ? 'badge-warning' : p === 'DIRECT' ? 'badge-success' : 'badge-primary'}" style="margin:2px;">${escapeHtml(displayName)}</span>`;
  }).join(" ") || `<span style="font-size:12px;color:var(--text2);">${I18N["group.no_policy"]}</span>`;

  main.innerHTML = `
    <div class="card" style="margin-bottom:16px;">
      <div class="card-header">
        <div>
          <h3 style="display:flex;align-items:center;gap:8px;">
            ${escapeHtml(translateName(g.name))}
            <span class="badge badge-primary">${g.group_type}</span>
          </h3>
          <p style="font-size:12px;color:var(--text2);margin-top:6px;">${I18N["group.policies"]}: ${policyTags}</p>
        </div>
        <div class="px-info-actions">
          <button class="btn btn-sm btn-outline" onclick='editService(${JSON.stringify(g)})'>${I18N["btn.edit"]}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteService(${g.id}, '${escapeHtml(g.name)}')">${I18N["btn.delete"]}</button>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <h3>${I18N["proxies.pinned_members"]} <span id="member-count" class="badge badge-primary" style="margin-left:4px;">0</span></h3>
        <button class="btn btn-primary btn-sm" onclick="openServicePinNodes(${g.id})">${I18N["proxies.pin_nodes"]}</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>${I18N["node.col_name"]}</th>
              <th>${I18N["node.col_sub"]}</th>
              <th>${I18N["node.col_region"]}</th>
              <th>${I18N["node.col_action"]}</th>
            </tr>
          </thead>
          <tbody id="member-table"></tbody>
        </table>
      </div>
      <div id="empty-state" class="empty" style="display:none;">
        <div class="icon">📌</div>
        <p>${I18N["node.empty"]}</p>
      </div>
    </div>`;
  loadServiceMembers(g.name);
}

async function loadServiceMembers(name) {
  try {
    showLoading();
    const nodes = await API.get(`/api/nodes?pinned_to=${encodeURIComponent(name)}`);
    const countEl = document.getElementById("member-count");
    if (countEl) countEl.textContent = nodes.length;
    const tbody = document.getElementById("member-table");
    const empty = document.getElementById("empty-state");
    if (nodes.length === 0) {
      tbody.innerHTML = "";
      empty.style.display = "block";
      hideLoading();
      return;
    }
    empty.style.display = "none";
    tbody.innerHTML = nodes.map(n => nodeRowLite(n, `
      <button class="btn btn-sm btn-danger" onclick="unpinFromService(${n.id}, '${escapeHtml(name)}', this)">${I18N["proxies.unpin"]}</button>
    `)).join("");
    compactActions();
    hideLoading();
  } catch (e) { toast(e.message, "error"); hideLoading(); }
}

async function unpinFromService(nodeId, name, btn) {
  await withLoading(btn, async () => {
    try {
      await API.put("/api/nodes/batch/pin", { node_ids: [nodeId], service_name: name, pinned: false });
      toast(I18N["node.unpinned"].replace("{0}", 1));
      await refreshAll();
    } catch (e) { toast(e.message, "error"); }
  });
}

function openServicePinNodes(groupId) {
  const g = allServiceGroups.find(s => s.id === groupId);
  if (!g) return;
  const pinned = new Set(allNodesFlat().filter(n => (n.pinned_services || []).includes(g.name)).map(n => n.id));
  openNodePicker(I18N["proxies.pin_nodes"], pinned, async ids => {
    await API.put("/api/nodes/batch/pin", { node_ids: ids, service_name: g.name, pinned: true });
    toast(I18N["node.pinned_to"].replace("{0}", translateName(g.name)));
    await refreshAll();
  });
}

// ===== 节点多选弹窗（地区添加成员 / 服务钉选共用） =====

function openNodePicker(title, excludeIds, onConfirm) {
  const list = document.getElementById("node-picker-list");
  document.getElementById("node-picker-title").textContent = title;
  document.getElementById("node-picker-search").value = "";
  const candidates = allNodesFlat().filter(n => !excludeIds.has(n.id));
  const row = n => `
    <label class="picker-row" data-name="${escapeHtml(n.name.toLowerCase())}"
      style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;cursor:pointer;">
      <input type="checkbox" class="picker-check" value="${n.id}">
      <span style="width:6px;height:6px;border-radius:50%;background:${n.subscription_color || '#6c5ce7'};flex-shrink:0;"></span>
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(n.name)}</span>
      <span style="margin-left:auto;font-size:11px;color:var(--text2);flex-shrink:0;">${escapeHtml(translateName(n.region_manual || n.region_auto || I18N["node.no_region"]))}</span>
    </label>`;
  list.innerHTML = candidates.length
    ? candidates.map(row).join("")
    : `<span style="font-size:12px;color:var(--text2);">${I18N["proxies.node_picker_empty"]}</span>`;
  document.getElementById("node-picker-confirm").onclick = async () => {
    const btn = document.getElementById("node-picker-confirm");
    const ids = Array.from(list.querySelectorAll(".picker-check:checked")).map(cb => parseInt(cb.value));
    if (ids.length === 0) {
      toast(I18N["proxies.node_picker_empty"], "error");
      return;
    }
    await withLoading(btn, async () => {
      try {
        await onConfirm(ids);
        hideModal("node-picker-modal");
      } catch (e) { toast(e.message, "error"); }
    });
  };
  showModal("node-picker-modal");
}

function filterPickerList() {
  const q = (document.getElementById("node-picker-search").value || "").trim().toLowerCase();
  document.querySelectorAll("#node-picker-list .picker-row").forEach(el => {
    el.style.display = !q || el.dataset.name.includes(q) ? "" : "none";
  });
}

// ===== 节点行渲染 =====

function regionBadgeHtml(n) {
  return n.region_manual
    ? `<span class="badge badge-warning">${I18N["node.manual_label"].replace("{0}", escapeHtml(n.region_manual))}</span>`
    : n.region_auto
      ? `<span class="badge badge-primary">${I18N["node.auto_label"].replace("{0}", escapeHtml(n.region_auto))}</span>`
      : '<span style="color:var(--text2)">-</span>';
}

function pinnedBadgesHtml(n) {
  return (n.pinned_services || []).map(s =>
    `<span class="badge badge-success" style="margin:1px;">${escapeHtml(translateName(s))}</span>`
  ).join("") || '<span style="color:var(--text2)">-</span>';
}

function nodeNameCell(n) {
  const subColor = n.subscription_color || '#6c5ce7';
  return `
    <td>
      <div style="display:flex;align-items:center;gap:6px;">
        <span style="width:6px;height:6px;border-radius:50%;background:${subColor};flex-shrink:0;"></span>
        <a href="javascript:void(0)" onclick="showDetail(${n.id})" style="color:var(--primary);text-decoration:none;">${escapeHtml(n.name)}</a>
      </div>
    </td>`;
}

function subTagCell(n) {
  const subColor = n.subscription_color || '#6c5ce7';
  const subTag = n.subscription_tag || n.subscription_name || '';
  return `<td><span style="font-size:11px;color:${subColor};">${escapeHtml(subTag)}</span></td>`;
}

function nodeRowFull(n) {
  return `<tr>
    <td><input type="checkbox" class="node-check" data-id="${n.id}" ${selectedNodes.has(n.id) ? 'checked' : ''}></td>
    ${nodeNameCell(n)}
    ${subTagCell(n)}
    <td><span class="badge badge-primary" style="font-size:10px;">${n.node_type}</span></td>
    <td>${regionBadgeHtml(n)}</td>
    <td>${pinnedBadgesHtml(n)}</td>
    <td>
      <button class="btn btn-sm btn-outline" onclick="pinToService(${n.id})">${I18N["node.pin_btn"]}</button>
    </td>
  </tr>`;
}

function nodeRowLite(n, actionBtnHtml) {
  return `<tr>
    ${nodeNameCell(n)}
    ${subTagCell(n)}
    <td>${regionBadgeHtml(n)}</td>
    <td>${actionBtnHtml}</td>
  </tr>`;
}

// ===== 节点详情 / 批量操作（nodes.js 平移） =====

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
  if (!bar) return;
  if (selectedNodes.size > 0) {
    bar.style.display = "flex";
    document.getElementById("batch-count").textContent = I18N["node.selected_count"].replace("{0}", selectedNodes.size);
  } else {
    bar.style.display = "none";
  }
}

function clearSelection() {
  selectedNodes.clear();
  const selAll = document.getElementById("select-all");
  if (selAll) selAll.checked = false;
  document.querySelectorAll(".node-check").forEach(cb => cb.checked = false);
  updateBatchBar();
}

async function batchMove(btn) {
  const target = document.getElementById("batch-target").value;
  if (!target || selectedNodes.size === 0 || target.startsWith("pin:")) return;
  await withLoading(btn, async () => {
    try {
      await API.put("/api/nodes/batch/region", { node_ids: Array.from(selectedNodes), region_manual: target });
      toast(I18N["node.moved"].replace("{0}", selectedNodes.size).replace("{1}", translateName(target)));
      clearSelection();
      await refreshAll();
    } catch (e) { toast(e.message, "error"); }
  });
}

// ===== 钉选服务弹窗（nodes.js 平移） =====

function openPinModal(mode, nodeIds, onDone) {
  const list = document.getElementById("pin-service-list");
  const rows = allServiceGroups.map(s => `
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
          const every = allServiceGroups.map(s => s.name);
          for (const svc of every) {
            await API.put("/api/nodes/batch/pin", { node_ids: Array.from(nodeIds), service_name: svc, pinned: false });
          }
        } else {
          for (const svc of services) {
            await API.put("/api/nodes/batch/pin", { node_ids: Array.from(nodeIds), service_name: svc, pinned: mode === "pin" });
          }
        }
        toast(mode === "pin"
          ? I18N["node.pinned_to"].replace("{0}", services.map(translateName).join(", "))
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
  await withLoading(btn, () => openPinModal("pin", new Set(selectedNodes), () => { clearSelection(); refreshAll(); }));
}

async function batchUnpin(btn) {
  if (selectedNodes.size === 0) return;
  await withLoading(btn, () => openPinModal("unpin", new Set(selectedNodes), () => { clearSelection(); refreshAll(); }));
}

function pinToService(nodeId) {
  openPinModal("pin", new Set([nodeId]), () => refreshAll());
}

async function autoAssign(btn) {
  await withLoading(btn, async () => {
    try {
      toast(I18N["node.assigning"]);
      const result = await API.post("/api/nodes/auto-assign");
      toast(I18N["node.assigned"].replace("{0}", result.assigned));
      await refreshAll();
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

// ===== 订阅 CRUD（subscriptions.js 平移） =====

function showAddModal() {
  document.getElementById("modal-title").textContent = I18N["btn.add"];
  document.getElementById("edit-id").value = "";
  document.getElementById("sub-name").value = "";
  document.getElementById("sub-tag").value = "";
  document.getElementById("sub-color").value = "#6c5ce7";
  document.getElementById("sub-url").value = "";
  document.getElementById("sub-enabled").checked = true;
  document.getElementById("sub-auto-update").checked = true;
  document.getElementById("sub-interval").value = 86400;
  showModal("sub-modal");
}

function editSub(s) {
  document.getElementById("modal-title").textContent = I18N["btn.edit"];
  document.getElementById("edit-id").value = s.id;
  document.getElementById("sub-name").value = s.name;
  document.getElementById("sub-tag").value = s.tag || "";
  document.getElementById("sub-color").value = s.color || "#6c5ce7";
  document.getElementById("sub-url").value = s.url;
  document.getElementById("sub-enabled").checked = s.enabled;
  showModal("sub-modal");
}

async function saveSubscription() {
  const id = document.getElementById("edit-id").value;
  const nameEl = document.getElementById("sub-name");
  const urlEl = document.getElementById("sub-url");
  if (!nameEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(nameEl); return;
  }
  if (!urlEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(urlEl); return;
  }
  const data = {
    name: nameEl.value.trim(),
    tag: document.getElementById("sub-tag").value || null,
    color: document.getElementById("sub-color").value,
    url: urlEl.value.trim(),
    enabled: document.getElementById("sub-enabled").checked,
    auto_update: document.getElementById("sub-auto-update").checked,
    update_interval: parseInt(document.getElementById("sub-interval").value) || 86400,
  };

  try {
    if (id) {
      await API.put(`/api/subscriptions/${id}`, data);
      toast(I18N["msg.updated"]);
    } else {
      await API.post("/api/subscriptions", data);
      toast(I18N["msg.added"]);
    }
    hideModal("sub-modal");
    refreshAll();
  } catch (e) { toast(e.message, "error"); }
}

async function syncOne(id, btn) {
  await withLoading(btn, async () => {
    try {
      showLoading();
      const result = await API.post(`/api/subscriptions/${id}/sync`);
      toast(I18N["sub.sync_done"].replace("{0}", result.subscription).replace("{1}", result.node_count));
      await refreshAll();
    } catch (e) { toast(e.message, "error"); }
    finally { hideLoading(); }
  });
}

async function syncAll(btn) {
  await withLoading(btn, async () => {
    try {
      showLoading();
      const result = await API.post("/api/subscriptions/sync-all");
      toast(I18N["sub.sync_all_done"].replace("{0}", result.results.length));
      await refreshAll();
    } catch (e) { toast(e.message, "error"); }
    finally { hideLoading(); }
  });
}

async function deleteSub(id, name) {
  if (!(await confirmDialog(I18N["sub.confirm_delete"].replace("{0}", name), { danger: true }))) return;
  try {
    await API.del(`/api/subscriptions/${id}`);
    toast(I18N["msg.deleted"]);
    if (selection === `sub:${id}`) setSelection("all");
    else refreshAll();
  } catch (e) { toast(e.message, "error"); }
}

// ===== 地区/服务组 CRUD（groups.js 平移） =====

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
    await refreshAll();
  } catch (e) { toast(e.message, "error"); }
}

async function toggleRegionMode(id, field, value) {
  try {
    await API.put(`/api/groups/regions/${id}`, { [field]: value });
    await reloadGroups();
    renderSidebar();
    renderMain();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteRegion(id, name) {
  if (!(await confirmDialog(I18N["group.confirm_delete_region"].replace("{0}", name), { danger: true }))) return;
  try {
    await API.del(`/api/groups/regions/${id}`);
    toast(I18N["msg.deleted"]);
    if (selection === `region:${id}`) setSelection("all");
    else await refreshAll();
  } catch (e) { toast(e.message, "error"); }
}

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
  const autoRegions = allRegionGroups.map(r => ({ name: r.name, label: I18N["group.auto_suffix"].replace("{0}", translateName(r.name)), type: "auto" }));
  const manualRegions = allRegionGroups.map(r => ({ name: `${r.name}手动`, label: I18N["group.manual_suffix"].replace("{0}", translateName(r.name)), type: "manual" }));
  return { virtual, autoRegions, manualRegions };
}

function renderPolicyTags() {
  const container = document.getElementById("policy-tags");
  if (currentPolicies.length === 0) {
    container.innerHTML = `<span style="color:var(--text2);font-size:12px;">${I18N["group.no_policy"]}</span>`;
    return;
  }
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
    new Sortable(container, {
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
    await refreshAll();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteService(id, name) {
  if (!(await confirmDialog(I18N["group.confirm_delete_service"].replace("{0}", name), { danger: true }))) return;
  try {
    await API.del(`/api/groups/services/${id}`);
    toast(I18N["msg.deleted"]);
    if (selection === `service:${id}`) setSelection("all");
    else await refreshAll();
  } catch (e) { toast(e.message, "error"); }
}

// ===== 启动 =====

async function init() {
  selection = parseHash();
  await loadCore();
  renderSidebar();
  renderMain();
}
init();
