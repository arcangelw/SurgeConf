/** 系统设置页：语言切换、访问控制、系统配置批量保存、数据重置 */

async function setLocale(locale) {
  try {
    await API.post("/api/settings/locale", { locale });
    window.location.reload();
  } catch (e) { toast(e.message, "error"); }
}

async function resetData() {
  if (!(await confirmDialog(I18N["settings.confirm_reset"], { danger: true }))) return;
  try {
    await API.post("/api/settings/reset");
    toast(I18N["settings.reset_done"]);
    setTimeout(() => window.location.reload(), 1000);
  } catch (e) { toast(e.message, "error"); }
}

// ===== 访问控制（本机 / 局域网登录） =====

let accessConfigured = false;

async function loadAccess() {
  try {
    const s = await API.get("/api/auth/session");
    if (!s.authenticated) return;
    accessConfigured = !!s.password_configured;
    document.getElementById("access-mode").value = s.admin_access;
    updateAccessUI();
  } catch (e) { /* 静默：设置页本身已受访问矩阵保护 */ }
}

function updateAccessUI() {
  const mode = document.getElementById("access-mode").value;
  const hint = document.getElementById("access-mode-hint");
  const current = document.getElementById("access-current-group");
  hint.textContent = mode === "local_only"
    ? I18N["settings.access_local_hint"]
    : I18N["settings.access_lan_hint"];
  current.style.display = accessConfigured ? "block" : "none";
}

async function saveAccess(btn) {
  const mode = document.getElementById("access-mode").value;
  const current = document.getElementById("access-current-password").value || null;
  const next = document.getElementById("access-new-password").value || null;
  if (mode === "lan_authenticated" && !accessConfigured && !next) {
    toast(I18N["settings.access_password_required"], "error");
    return;
  }
  await withLoading(btn, async () => {
    try {
      await API.put("/api/settings/access", {
        installation_role: "home_gateway",
        admin_access: mode,
        current_password: current,
        new_password: next,
      });
      toast(I18N["settings.access_saved"]);
      document.getElementById("access-current-password").value = "";
      document.getElementById("access-new-password").value = "";
      await loadAccess();
    } catch (e) { toast(e.message, "error"); }
  });
}

// ===== 系统级配置 =====

async function loadSysConfig() {
  try {
    const config = await API.get("/api/settings/config");
    const keywords = config.exclusion_keywords || "Remain|Expired|官网|如需|套餐|去除|剩余|距离|Reset|重置|流量|Traffic|Used|已用|Today|今日|GB|Download|Upload|Speed";
    document.getElementById("exclusion-keywords").value = keywords.replace(/\|/g, "\n");
    document.getElementById("other-auto-enabled").checked = config.other_auto_enabled !== "false";
    document.getElementById("other-manual-enabled").checked = config.other_manual_enabled !== "false";
  } catch (e) { toast(e.message, "error"); }
}

async function saveSysConfig(btn) {
  await withLoading(btn, async () => {
    try {
      const keywords = document.getElementById("exclusion-keywords").value
        .split("\n").map(s => s.trim()).filter(Boolean).join("|");
      const configs = {
        exclusion_keywords: keywords,
        other_auto_enabled: document.getElementById("other-auto-enabled").checked ? "true" : "false",
        other_manual_enabled: document.getElementById("other-manual-enabled").checked ? "true" : "false",
      };
      await API.put("/api/settings/config/batch", { configs });
      toast(I18N["settings.saved"]);
    } catch (e) { toast(e.message, "error"); }
  });
}

// ===== 全局 Skip-Proxy（general.js 平移） =====

let skipProxyItems = [];

async function loadSkipProxy() {
  try {
    const data = await API.get("/api/general/skip-proxy");
    skipProxyItems = data.custom || [];
    renderSkipProxy();
    const defaultsEl = document.getElementById("skip-proxy-defaults");
    defaultsEl.innerHTML = (data.defaults || []).map(d =>
      `<span class="badge">${escapeHtml(d)}</span>`
    ).join("");
  } catch (e) { toast(e.message, "error"); }
}

function renderSkipProxy() {
  const el = document.getElementById("skip-proxy-list");
  if (!skipProxyItems.length) {
    el.innerHTML = `<span style="font-size:12px;color:var(--text2);">${I18N["general.skip_proxy_empty"]}</span>`;
    return;
  }
  el.innerHTML = skipProxyItems.map((item, i) =>
    `<span class="badge badge-primary" style="display:inline-flex;align-items:center;gap:4px;">${escapeHtml(item)} <a style="cursor:pointer;margin-left:2px;color:var(--text2);" onclick="removeSkipProxyItem(${i})">✕</a></span>`
  ).join("");
}

async function addSkipProxyItem() {
  const input = document.getElementById("skip-proxy-input");
  const val = input.value.trim();
  if (!val) {
    toast(I18N["msg.required"], "error"); flashInput(input); return;
  }
  if (skipProxyItems.includes(val)) { toast(I18N["general.skip_proxy_dup"], "error"); return; }
  skipProxyItems.push(val);
  input.value = "";
  renderSkipProxy();
  await saveSkipProxy();
}

async function removeSkipProxyItem(i) {
  skipProxyItems.splice(i, 1);
  renderSkipProxy();
  await saveSkipProxy();
}

async function saveSkipProxy() {
  try {
    await API.put("/api/general/skip-proxy", { items: skipProxyItems });
    toast(I18N["msg.saved"]);
  } catch (e) { toast(e.message, "error"); }
}

loadSkipProxy();

loadSysConfig();

loadAccess();
