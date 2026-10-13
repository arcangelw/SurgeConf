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

/** 解析 "TYPE,value,action" 文本为结构化对象；非简单类型返回 null */
function parseRuleText(text) {
  const parts = text.split(",");
  if (parts.length >= 3 && SIMPLE_RULE_TYPES.includes(parts[0])) {
    return { type: parts[0], value: parts.slice(1, -1).join(","), action: parts[parts.length - 1] };
  }
  return null;
}
