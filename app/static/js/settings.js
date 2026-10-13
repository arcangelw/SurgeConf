/** 系统设置页：语言切换、系统配置批量保存、数据重置（由 settings.html 内联 JS 迁出） */

async function setLocale(locale) {
  try {
    await fetch("/api/settings/locale", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ locale }),
    });
    window.location.reload();
  } catch (e) { toast(e.message, "error"); }
}

async function resetData() {
  if (!(await confirmDialog(I18N["settings.confirm_reset"], { danger: true }))) return;
  try {
    const resp = await fetch("/api/settings/reset", { method: "POST" });
    if (resp.ok) {
      toast(I18N["settings.reset_done"]);
      setTimeout(() => window.location.reload(), 1000);
    }
  } catch (e) { toast(e.message, "error"); }
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

loadSysConfig();
