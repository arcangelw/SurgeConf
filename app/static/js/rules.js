/** 规则管理页：规则源/自定义规则/Host 映射（由 rules.html 内联 JS 迁出；弹窗交互共用 rules-common.js setupRuleModals） */

let allRules = [];
let allRegions = [];
let allServices = [];
let allCustomRules = {pre: [], general: []};
let defaultDnsPresets = [];

// ===== Tab 切换（localStorage 保持） =====

function switchRulesTab(tab) {
  document.querySelectorAll("#rules-tab-bar .tab-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  for (const t of ["sources", "custom", "hosts"]) {
    const el = document.getElementById(`rules-tab-${t}`);
    if (el) el.style.display = t === tab ? "" : "none";
  }
  try { localStorage.setItem("rules-active-tab", tab); } catch { /* 隐私模式忽略 */ }
}


const CATEGORY_MAP = {
  unbreak: I18N["rule.cat.unbreak"],
  adblock: I18N["rule.cat.adblock"],
  mainland_app: I18N["rule.cat.mainland_app"],
  apple: I18N["rule.cat.apple"],
  ai: I18N["rule.cat.ai"],
  google: I18N["rule.cat.google"],
  streaming: I18N["rule.cat.streaming"],
  region_stream: I18N["rule.cat.region_stream"],
  social: I18N["rule.cat.social"],
  intl_service: I18N["rule.cat.intl_service"],
  dev: I18N["rule.cat.dev"],
  mainland: I18N["rule.cat.mainland"],
  global: I18N["rule.cat.global"],
  lan: I18N["rule.cat.lan"],
};

function displayAction(name) {
  return VIRT_LABELS[name] || translateName(name);
}

function buildCategoryOptions() {
  let html = '<option value="">--</option>';
  for (const [key, label] of Object.entries(CATEGORY_MAP)) {
    html += `<option value="${key}">${label}</option>`;
  }
  return html;
}

async function loadGroups() {
  try {
    const [regions, services] = await Promise.all([
      API.get("/api/groups/regions"),
      API.get("/api/groups/services"),
    ]);
    allRegions = regions.filter(r => r.enabled);
    allServices = services.filter(s => s.enabled);
  } catch (e) { /* ignore */ }
}

async function loadData() {
  try {
    allRules = await API.get("/api/configs/rules");
    document.getElementById("rule-count").textContent = I18N["rule.total"].replace("{0}", allRules.length);
    renderTable();
    enableSortable("rule-list", (items) => {
      allRules = items.map(item => allRules.find(r => r.id === item.id) || item);
      saveReorder(items);
    });
  } catch (e) { toast(e.message, "error"); }
}

async function loadCustomRules() {
  try {
    const rules = await API.get("/api/configs/custom-rules");
    allCustomRules.pre = rules.filter(r => r.position === "pre");
    allCustomRules.general = rules.filter(r => r.position === "general");
    renderCustomRuleTable("pre");
    renderCustomRuleTable("general");
    enableSortable("pre-rule-list", (items) => {
      allCustomRules.pre = items.map(item => allCustomRules.pre.find(r => r.id === item.id) || item);
      saveCustomReorder(items);
    });
    enableSortable("general-rule-list", (items) => {
      allCustomRules.general = items.map(item => allCustomRules.general.find(r => r.id === item.id) || item);
      saveCustomReorder(items);
    });
    document.getElementById("pre-count").textContent = I18N["rule.total"].replace("{0}", allCustomRules.pre.length);
    document.getElementById("general-count").textContent = I18N["rule.total"].replace("{0}", allCustomRules.general.length);
  } catch (e) { toast(e.message, "error"); }
}

function renderTable() {
  const tbody = document.getElementById("rule-list");
  tbody.innerHTML = allRules.map((r) => `
    <tr data-id="${r.id}">
      <td style="cursor:grab;color:var(--text2);font-size:14px;">⠿</td>
      <td><strong>${escapeHtml(r.name)}</strong></td>
      <td><span class="badge badge-primary">${r.rule_type}</span></td>
      <td><span class="badge badge-success">${escapeHtml(displayAction(r.action))}</span></td>
      <td>${categoryLabel(r.category)}</td>
      <td style="font-size:12px;color:var(--text2);">${r.sort_order}</td>
      <td><span class="badge ${r.enabled ? 'badge-success' : 'badge-danger'}">${r.enabled ? I18N["btn.enable"] : I18N["btn.disable"]}</span></td>
      <td>
        <button class="btn btn-sm btn-outline" onclick='editRule(${JSON.stringify(r)})'>${I18N["btn.edit"]}</button>
        <button class="btn btn-sm ${r.enabled ? 'btn-outline' : 'btn-success'}" onclick="toggleRule(${r.id}, ${!r.enabled})">${r.enabled ? I18N["btn.disable"] : I18N["btn.enable"]}</button>
        <button class="btn btn-sm btn-danger" onclick="deleteRule(${r.id})">${I18N["btn.delete"]}</button>
      </td>
    </tr>
  `).join("");
  compactActions();
}

function renderCustomRuleTable(position) {
  const rules = allCustomRules[position];
  const tbody = document.getElementById(position + "-rule-list");
  tbody.innerHTML = rules.map((r) => `
    <tr data-id="${r.id}">
      <td style="cursor:grab;color:var(--text2);font-size:14px;">⠿</td>
      <td><code style="font-size:12px;">${escapeHtml(r.rule_text)}</code></td>
      <td style="font-size:12px;color:var(--text2);">${escapeHtml(r.comment || '-')}</td>
      <td><span class="badge ${r.enabled ? 'badge-success' : 'badge-danger'}">${r.enabled ? I18N["btn.enable"] : I18N["btn.disable"]}</span></td>
      <td>
        <button class="btn btn-sm btn-outline" onclick='editCustomRule(${JSON.stringify(r)})'>${I18N["btn.edit"]}</button>
        <button class="btn btn-sm ${r.enabled ? 'btn-outline' : 'btn-success'}" onclick="toggleCustomRule(${r.id}, ${!r.enabled})">${r.enabled ? I18N["btn.disable"] : I18N["btn.enable"]}</button>
        <button class="btn btn-sm btn-danger" onclick="deleteCustomRule(${r.id}, null, '${position}')">${I18N["btn.delete"]}</button>
      </td>
    </tr>
  `).join("");
  compactActions();
}

function categoryLabel(cat) {
  return CATEGORY_MAP[cat] || cat || "-";
}

async function saveReorder(updates) {
  try {
    await API.put("/api/configs/rules/reorder", updates);
    toast(I18N["rule.order_saved"]);
  } catch (e) { toast(e.message, "error"); }
}

async function saveCustomReorder(updates) {
  try {
    await API.put("/api/configs/custom-rules/reorder", updates);
    toast(I18N["rule.order_saved"]);
  } catch (e) { toast(e.message, "error"); }
}

async function resetDefaultOrder() {
  if (!(await confirmDialog(I18N["rule.confirm_reset"], { danger: true }))) return;
  try {
    const [rules, defaultOrder] = await Promise.all([
      API.get("/api/configs/rules"),
      API.get("/api/configs/rules/default-order"),
    ]);
    const names = defaultOrder.map(d => d.name);
    const updates = rules
      .filter(r => names.includes(r.name))
      .map(r => ({ id: r.id, sort_order: names.indexOf(r.name) }))
      .concat(
        rules.filter(r => !names.includes(r.name))
          .map((r, i) => ({ id: r.id, sort_order: names.length + i }))
      );
    await API.put("/api/configs/rules/reorder", updates);
    toast(I18N["rule.reset_done"]);
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

// ===== 规则源 CRUD =====

function showAddRule() {
  document.getElementById("rule-modal-title").textContent = I18N["rule.add"];
  document.getElementById("rule-edit-id").value = "";
  document.getElementById("rule-name").value = "";
  document.getElementById("rule-url").value = "";
  document.getElementById("rule-type").value = "RULE-SET";
  document.getElementById("rule-action").innerHTML = buildActionOptions("", allServices, allRegions);
  document.getElementById("rule-category").value = "";
  document.getElementById("rule-order").value = 50;
  showModal("rule-modal");
}

function editRule(r) {
  document.getElementById("rule-modal-title").textContent = I18N["btn.edit"];
  document.getElementById("rule-edit-id").value = r.id;
  document.getElementById("rule-name").value = r.name;
  document.getElementById("rule-url").value = r.url;
  document.getElementById("rule-type").value = r.rule_type;
  document.getElementById("rule-action").innerHTML = buildActionOptions(r.action, allServices, allRegions);
  document.getElementById("rule-category").value = r.category || "";
  document.getElementById("rule-order").value = r.sort_order;
  showModal("rule-modal");
}

async function saveRule() {
  const id = document.getElementById("rule-edit-id").value;
  const nameEl = document.getElementById("rule-name");
  const urlEl = document.getElementById("rule-url");
  if (!nameEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(nameEl); return;
  }
  if (!urlEl.value.trim()) {
    toast(I18N["msg.required"], "error"); flashInput(urlEl); return;
  }
  const data = {
    name: nameEl.value.trim(),
    url: urlEl.value.trim(),
    rule_type: document.getElementById("rule-type").value,
    action: document.getElementById("rule-action").value,
    category: document.getElementById("rule-category").value || null,
    sort_order: parseInt(document.getElementById("rule-order").value) || 0,
  };
  try {
    if (id) {
      await API.put(`/api/configs/rules/${id}`, data);
      toast(I18N["msg.updated"]);
    } else {
      await API.post("/api/configs/rules", data);
      toast(I18N["msg.added"]);
    }
    hideModal("rule-modal");
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

async function toggleRule(id, enabled) {
  try {
    await API.put(`/api/configs/rules/${id}`, { enabled });
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteRule(id) {
  if (!(await confirmDialog(I18N["rule.confirm_delete"], { danger: true }))) return;
  try {
    await API.del(`/api/configs/rules/${id}`);
    toast(I18N["msg.deleted"]);
    loadData();
  } catch (e) { toast(e.message, "error"); }
}

// 自定义规则启用/禁用（弹窗交互在 setupRuleModals）
async function toggleCustomRule(id, enabled) {
  try {
    await API.put(`/api/configs/custom-rules/${id}`, { enabled });
    loadCustomRules();
  } catch (e) { toast(e.message, "error"); }
}

// ===== 默认 Host 映射列表 =====

async function loadDnsPresets() {
  try {
    defaultDnsPresets = await API.get("/api/configs/host-presets");
  } catch (e) { /* ignore */ }
}

async function loadHostMappings() {
  try {
    const mappings = await API.get("/api/configs/host-mappings");
    document.getElementById("host-count").textContent = I18N["rule.total"].replace("{0}", mappings.length);
    const tbody = document.getElementById("host-list");
    tbody.innerHTML = mappings.map(m => `
      <tr>
        <td><code style="font-size:12px;">${escapeHtml(m.domain)}</code></td>
        <td><span class="badge badge-primary">${escapeHtml(m.target)}</span></td>
        <td><span class="badge ${m.enabled ? 'badge-success' : 'badge-danger'}">${m.enabled ? I18N["btn.enable"] : I18N["btn.disable"]}</span></td>
        <td>
          <button class="btn btn-sm btn-outline" onclick='editHost(${JSON.stringify(m)})'>${I18N["btn.edit"]}</button>
          <button class="btn btn-sm ${m.enabled ? 'btn-outline' : 'btn-success'}" onclick="toggleHost(${m.id}, ${!m.enabled})">${m.enabled ? I18N["btn.disable"] : I18N["btn.enable"]}</button>
          <button class="btn btn-sm btn-danger" onclick="deleteHost(${m.id}, null)">${I18N["btn.delete"]}</button>
        </td>
      </tr>
    `).join("");
    compactActions();
  } catch (e) { toast(e.message, "error"); }
}

async function toggleHost(id, enabled) {
  try {
    await API.put(`/api/configs/host-mappings/${id}`, { enabled });
    loadHostMappings();
  } catch (e) { toast(e.message, "error"); }
}

// ===== 启动 =====

setupRuleModals({
  getServices: () => allServices,
  getRegions: () => allRegions,
  getDnsPresets: () => defaultDnsPresets,
  getNextSortOrder: (position) => allCustomRules[position].length,
  onRuleSaved: () => loadCustomRules(),
  onHostSaved: () => loadHostMappings(),
});

async function init() {
  document.getElementById("rule-category").innerHTML = buildCategoryOptions();
  let savedTab = "sources";
  try { savedTab = localStorage.getItem("rules-active-tab") || "sources"; } catch { /* ignore */ }
  switchRulesTab(savedTab);
  await loadGroups();
  await loadDnsPresets();
  loadData();
  loadCustomRules();
  loadHostMappings();
}
init();
