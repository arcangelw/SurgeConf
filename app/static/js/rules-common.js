/** 规则/策略公共常量与纯函数（rules 与 profiles 页共用，零 DOM 依赖）
 *  依赖：app.js（escapeHtml/translateName）与 base.html 注入的 window.I18N */

const VIRT_LABELS = {
  "全部节点": I18N["group.virt.all_nodes"],
  "自动选择": I18N["group.virt.auto"],
  "代理": I18N["group.virt.proxy"],
  "手动选择": I18N["group.virt.manual_select"],
  "大陆直连": I18N["group.virt.mainland"],
  "兜底": I18N["group.virt.fallback"],
};

const RULE_TYPE_LABELS = {
  "DOMAIN": I18N["rule.value_domain"],
  "DOMAIN-SUFFIX": I18N["rule.value_domain"],
  "DOMAIN-KEYWORD": I18N["rule.value_keyword"],
  "IP-CIDR": I18N["rule.value_ip"],
  "IP-CIDR6": I18N["rule.value_ipv6"],
  "URL-REGEX": I18N["rule.value_regex"],
  "PROCESS-NAME": I18N["rule.value_process"],
  "PROTOCOL": I18N["rule.value_protocol"],
};

const SIMPLE_RULE_TYPES = ["DOMAIN","DOMAIN-SUFFIX","DOMAIN-KEYWORD","IP-CIDR","IP-CIDR6","URL-REGEX","PROCESS-NAME","PROTOCOL"];

/** 构建规则动作下拉：系统动作 + 虚拟组 + 服务组 + 地区组（auto/manual） */
function buildActionOptions(selectedValue, services = [], regions = []) {
  const parts = [];

  const sysActions = ["DIRECT", "REJECT", "REJECT-DROP", "REJECT-NO-DROP"];
  parts.push(`<optgroup label="${I18N["rule.action_system"]}">`);
  for (const a of sysActions) {
    parts.push(`<option value="${a}" ${a === selectedValue ? 'selected' : ''}>${a}</option>`);
  }
  parts.push('</optgroup>');

  const virtGroups = ["全部节点", "自动选择", "代理", "手动选择", "大陆直连", "其他"];
  parts.push(`<optgroup label="${I18N["rule.action_virtual"]}">`);
  for (const g of virtGroups) {
    const label = VIRT_LABELS[g] || g;
    parts.push(`<option value="${escapeHtml(g)}" ${g === selectedValue ? 'selected' : ''}>${escapeHtml(label)}</option>`);
  }
  parts.push('</optgroup>');

  if (services.length > 0) {
    parts.push(`<optgroup label="${I18N["rule.action_service"]}">`);
    for (const s of services) {
      const label = translateName(s.name);
      parts.push(`<option value="${escapeHtml(s.name)}" ${s.name === selectedValue ? 'selected' : ''}>${escapeHtml(label)}</option>`);
    }
    parts.push('</optgroup>');
  }

  if (regions.length > 0) {
    parts.push(`<optgroup label="${I18N["rule.action_region_auto"]}">`);
    for (const r of regions) {
      parts.push(`<option value="${escapeHtml(r.name)}" ${r.name === selectedValue ? 'selected' : ''}>${escapeHtml(translateName(r.name))}</option>`);
    }
    parts.push('</optgroup>');

    parts.push(`<optgroup label="${I18N["rule.action_region_manual"]}">`);
    for (const r of regions) {
      const manName = r.name + "手动";
      const manLabel = translateName(r.name) + " Manual";
      parts.push(`<option value="${escapeHtml(manName)}" ${manName === selectedValue ? 'selected' : ''}>${escapeHtml(manLabel)}</option>`);
    }
    parts.push('</optgroup>');
  }

  return parts.join("");
}

/** 统一绑定自定义规则/Host 映射弹窗交互（rules 与 profiles 页共用，元素 id 见 macros/rule_modals.html）
 *  opts: { getServices, getRegions, getDnsPresets, getNextSortOrder, onRuleSaved(profileId, position), onHostSaved(profileId) } */
function setupRuleModals(opts = {}) {
  const getServices = () => (opts.getServices ? opts.getServices() : []);
  const getRegions = () => (opts.getRegions ? opts.getRegions() : []);
  const getDnsPresets = () => (opts.getDnsPresets ? opts.getDnsPresets() : []);

  const $ = (id) => document.getElementById(id);

  // ===== 自定义规则弹窗 =====

  function toggleCustomRuleMode() {
    const raw = $("custom-rule-raw-mode").checked;
    $("custom-rule-structured").style.display = raw ? "none" : "block";
    $("custom-rule-raw").style.display = raw ? "block" : "none";
  }

  function updateCustomRuleValueLabel() {
    const type = $("custom-rule-type").value;
    $("custom-rule-value-label").textContent = RULE_TYPE_LABELS[type] || I18N["rule.value_domain"];
  }

  $("custom-rule-raw-mode").addEventListener("change", toggleCustomRuleMode);
  $("custom-rule-type").addEventListener("change", updateCustomRuleValueLabel);

  window.showAddCustomRule = function(position, profileId = null) {
    $("custom-rule-modal-title").textContent =
      position === "pre" ? I18N["rule.add_pre"] : I18N["rule.add_general"];
    $("custom-rule-edit-id").value = "";
    $("custom-rule-profile-id").value = profileId ?? "";
    $("custom-rule-position").value = position;
    $("custom-rule-raw-mode").checked = false;
    $("custom-rule-structured").style.display = "block";
    $("custom-rule-raw").style.display = "none";
    $("custom-rule-type").value = "DOMAIN-SUFFIX";
    $("custom-rule-value").value = "";
    $("custom-rule-action").innerHTML = buildActionOptions("", getServices(), getRegions());
    $("custom-rule-text").value = "";
    $("custom-rule-comment").value = "";
    updateCustomRuleValueLabel();
    showModal("custom-rule-modal");
  };

  window.editCustomRule = function(r, profileId = null) {
    $("custom-rule-modal-title").textContent = I18N["btn.edit"];
    $("custom-rule-edit-id").value = r.id;
    $("custom-rule-profile-id").value = profileId ?? "";
    $("custom-rule-position").value = r.position;
    $("custom-rule-comment").value = r.comment || "";
    $("custom-rule-text").value = r.rule_text;

    const parsed = parseRuleText(r.rule_text);
    if (parsed) {
      $("custom-rule-raw-mode").checked = false;
      $("custom-rule-structured").style.display = "block";
      $("custom-rule-raw").style.display = "none";
      $("custom-rule-type").value = parsed.type;
      $("custom-rule-value").value = parsed.value;
      $("custom-rule-action").innerHTML = buildActionOptions(parsed.action, getServices(), getRegions());
      updateCustomRuleValueLabel();
    } else {
      $("custom-rule-raw-mode").checked = true;
      $("custom-rule-structured").style.display = "none";
      $("custom-rule-raw").style.display = "block";
    }
    showModal("custom-rule-modal");
  };

  window.saveCustomRule = async function() {
    const id = $("custom-rule-edit-id").value;
    const profileId = parseInt($("custom-rule-profile-id").value) || null;
    const position = $("custom-rule-position").value;
    const raw = $("custom-rule-raw-mode").checked;
    let rule_text;
    if (raw) {
      rule_text = $("custom-rule-text").value.trim();
      if (!rule_text) {
        toast(I18N["msg.required"], "error"); flashInput($("custom-rule-text")); return;
      }
    } else {
      const type = $("custom-rule-type").value;
      const value = $("custom-rule-value").value.trim();
      const action = $("custom-rule-action").value;
      if (!value) {
        toast(I18N["msg.required"], "error"); flashInput($("custom-rule-value")); return;
      }
      rule_text = `${type},${value},${action}`;
    }
    const data = {
      rule_text: rule_text,
      position: position,
      comment: $("custom-rule-comment").value.trim() || null,
    };
    try {
      if (id) {
        await API.put(`/api/configs/custom-rules/${id}`, data);
        toast(I18N["msg.updated"]);
      } else {
        if (profileId) data.profile_id = profileId;
        data.sort_order = opts.getNextSortOrder ? opts.getNextSortOrder(position) : 0;
        await API.post("/api/configs/custom-rules", data);
        toast(I18N["msg.added"]);
      }
      hideModal("custom-rule-modal");
      opts.onRuleSaved?.(profileId, position);
    } catch (e) { toast(e.message, "error"); }
  };

  window.deleteCustomRule = async function(id, profileId = null, position = null) {
    if (!(await confirmDialog(I18N["rule.confirm_delete"], { danger: true }))) return;
    try {
      await API.del(`/api/configs/custom-rules/${id}`);
      toast(I18N["msg.deleted"]);
      opts.onRuleSaved?.(profileId, position);
    } catch (e) { toast(e.message, "error"); }
  };

  // ===== Host 映射弹窗 =====

  function fillHostPresets(selectedValue) {
    const sel = $("host-target-preset");
    sel.innerHTML = `<option value="">${I18N["profile.host_select_preset"]}</option>` +
      getDnsPresets().map(p => `<option value="${p.value}">${p.label_zh}</option>`).join("");
    sel.value = getDnsPresets().find(p => p.value === selectedValue) ? selectedValue : "";
  }

  $("host-target-preset").addEventListener("change", function() {
    if (this.value) $("host-target").value = this.value;
  });

  window.showAddHost = function(profileId = null) {
    $("host-modal-title").textContent = I18N["profile.add_host"];
    $("host-edit-id").value = "";
    $("host-profile-id").value = profileId ?? "";
    $("host-domain").value = "";
    $("host-target").value = "";
    fillHostPresets("");
    $("host-skip-proxy").checked = true;
    showModal("host-modal");
  };

  window.editHost = function(m, profileId = null) {
    $("host-modal-title").textContent = I18N["btn.edit"];
    $("host-edit-id").value = m.id;
    $("host-profile-id").value = profileId ?? "";
    $("host-domain").value = m.domain;
    $("host-target").value = m.target;
    fillHostPresets(m.target);
    $("host-skip-proxy").checked = m.skip_proxy !== false;
    showModal("host-modal");
  };

  window.saveHost = async function() {
    const id = $("host-edit-id").value;
    const profileId = parseInt($("host-profile-id").value) || null;
    const domain = $("host-domain").value.trim();
    const presetTarget = $("host-target-preset").value;
    const customTarget = $("host-target").value.trim();
    const target = presetTarget || customTarget;
    const skipProxy = $("host-skip-proxy").checked;
    if (!domain) {
      toast(I18N["msg.required"], "error"); flashInput($("host-domain")); return;
    }
    if (!target) {
      toast(I18N["msg.required"], "error"); flashInput($("host-target")); return;
    }
    try {
      if (id) {
        await API.put(`/api/configs/host-mappings/${id}`, { domain, target, skip_proxy: skipProxy });
        toast(I18N["msg.updated"]);
      } else {
        await API.post("/api/configs/host-mappings", { profile_id: profileId, domain, target, skip_proxy: skipProxy, sort_order: 0 });
        toast(I18N["msg.added"]);
      }
      hideModal("host-modal");
      opts.onHostSaved?.(profileId);
    } catch (e) { toast(e.message, "error"); }
  };

  window.deleteHost = async function(id, profileId = null) {
    if (!(await confirmDialog(I18N["rule.confirm_delete"], { danger: true }))) return;
    try {
      await API.del(`/api/configs/host-mappings/${id}`);
      toast(I18N["msg.deleted"]);
      opts.onHostSaved?.(profileId);
    } catch (e) { toast(e.message, "error"); }
  };
}

/** 解析 "TYPE,value,action" 文本为结构化对象；非简单类型返回 null */
function parseRuleText(text) {
  const parts = text.split(",");
  if (parts.length >= 3 && SIMPLE_RULE_TYPES.includes(parts[0])) {
    return { type: parts[0], value: parts.slice(1, -1).join(","), action: parts[parts.length - 1] };
  }
  return null;
}
