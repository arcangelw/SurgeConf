/** 系统设置页：语言切换、访问控制、系统配置批量保存、备份生命周期、数据重置 */

async function setLocale(locale) {
  try {
    await API.post("/api/settings/locale", { locale });
    window.location.reload();
  } catch (e) { toast(e.message, "error"); }
}

async function resetData() {
  if (!(await confirmDialog(I18N["settings.confirm_reset"], { danger: true }))) return;
  try {
    await API.post("/api/settings/reset", { confirm: true });
    toast(I18N["settings.reset_done"]);
    setTimeout(() => window.location.reload(), 1000);
  } catch (e) { toast(e.message, "error"); }
}

// ===== 备份生命周期 =====

async function loadBackups() {
  try {
    renderBackups(await API.get("/api/settings/backups"));
  } catch (e) { /* 静默：设置页本身已受访问矩阵保护 */ }
}

function backupReasonLabel(reason) {
  return I18N[`settings.backup_reason_${reason}`] || reason;
}

function formatBackupTime(iso) {
  try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
}

function formatBytes(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

function renderBackups(list) {
  const body = document.getElementById("backups-tbody");
  if (!list.length) {
    body.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text2);">${I18N["settings.backup_empty"]}</td></tr>`;
    return;
  }
  body.innerHTML = list.map((b) => `
    <tr>
      <td>${escapeHtml(formatBackupTime(b.created_at))}</td>
      <td><code>${escapeHtml(b.schema_version)}</code></td>
      <td>${formatBytes(b.size)}</td>
      <td>${escapeHtml(backupReasonLabel(b.reason))}</td>
      <td style="white-space:nowrap;">
        <button class="btn btn-outline" onclick="downloadBackup('${escapeHtml(b.name)}')">${I18N["settings.backup_download"]}</button>
        <button class="btn btn-danger" onclick="restoreBackupFile('${escapeHtml(b.name)}')">${I18N["settings.backup_restore"]}</button>
      </td>
    </tr>`).join("");
}

async function createBackupNow(btn) {
  await withLoading(btn, async () => {
    try {
      await API.post("/api/settings/backups", {});
      toast(I18N["settings.backup_created"]);
      await loadBackups();
    } catch (e) { toast(e.message, "error"); }
  });
}

/** 下载走浏览器导航：先经同源 API 校验存在，避免把 404/HTML 当文件保存 */
async function downloadBackup(name) {
  try {
    const list = await API.get("/api/settings/backups");
    if (!list.some((b) => b.name === name)) {
      toast(I18N["settings.backup_missing"], "error");
      return;
    }
    window.open(`/api/settings/backups/${encodeURIComponent(name)}`, "_blank");
  } catch (e) { toast(e.message, "error"); }
}

async function restoreBackupFile(name) {
  if (!(await confirmDialog(I18N["settings.confirm_restore"], { danger: true }))) return;
  try {
    await API.post(`/api/settings/backups/${encodeURIComponent(name)}/restore`, { confirm: true });
    toast(I18N["settings.restore_done"]);
    setTimeout(() => window.location.reload(), 1000);
  } catch (e) { toast(e.message, "error"); }
}

// ===== 访问控制（本机 / 局域网登录） =====

let accessConfigured = false;
let accessLoaded = false;  // session 成功加载并回显后才允许提交

/** 访问控制表单控件集合：加载成功前禁用，防未回显状态下误提交 */
const ACCESS_FORM_IDS = ["installation-role", "access-mode",
  "access-current-password", "access-new-password", "access-save-btn"];

function setAccessFormDisabled(disabled) {
  for (const id of ACCESS_FORM_IDS) {
    const el = document.getElementById(id);
    if (el) el.disabled = disabled;
  }
}

async function loadAccess() {
  try {
    const s = await API.get("/api/auth/session");
    if (!s || !s.authenticated) {
      // 未认证（如 lan 模式登录态失效）：锁定表单并明确提示
      accessLoaded = false;
      setAccessFormDisabled(true);
      toast(I18N["settings.access_load_failed"], "error");
      return;
    }
    accessConfigured = !!s.password_configured;
    document.getElementById("access-mode").value = s.admin_access;
    // 实例角色回显：仅决定新建档案默认目标，非法/缺失值回落默认角色
    const roleSel = document.getElementById("installation-role");
    if (roleSel) roleSel.value = s.installation_role === "work_computer" ? "work_computer" : "home_gateway";
    updateAccessUI();
    accessLoaded = true;
    setAccessFormDisabled(false);
  } catch (e) {
    // 加载失败（含成功后再次失效）：明确报错并回锁表单——回显值已不可信，
    // saveAccess guard 同步复位，杜绝基于过期会话的提交
    accessLoaded = false;
    setAccessFormDisabled(true);
    toast(I18N["settings.access_load_failed"], "error");
  }
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
  // 会话未成功加载（表单禁用）时防御性拒绝：程序触发也不得提交
  if (!accessLoaded) {
    toast(I18N["settings.access_load_failed"], "error");
    return;
  }
  const mode = document.getElementById("access-mode").value;
  const roleSel = document.getElementById("installation-role");
  const role = roleSel ? roleSel.value : "home_gateway";
  const current = document.getElementById("access-current-password").value || null;
  const next = document.getElementById("access-new-password").value || null;
  if (mode === "lan_authenticated" && !accessConfigured && !next) {
    toast(I18N["settings.access_password_required"], "error");
    return;
  }
  await withLoading(btn, async () => {
    try {
      await API.put("/api/settings/access", {
        installation_role: role,
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

loadBackups();
