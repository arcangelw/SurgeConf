/** profiles.js General 强制字段（forced）只读行为测试（node vm + DOM stub）
 *
 * 覆盖（fix round 2）：
 *  - renderField：服务端 metadata forced === true 时 checkbox/select/textarea/
 *    number/text 全部 disabled + data-forced="1" + badge-warning 徽标；
 *    非 forced 字段不带任何禁用痕迹
 *  - renderGeneralForm：fieldMeta 含 forced 字段时容器 HTML 带禁用属性
 *  - collectGeneralValues：按 fieldMeta（服务端元数据）排除 forced 键——
 *    即使 DOM 被篡改（移除 disabled/data-forced、改危险值）也不提交；
 *    普通键与自定义 JSON 入口照常收集
 *  - fillGeneralForm：disabled 控件仍填充服务端强制值（展示）
 *
 * 运行：node tests/js/test_general_forced_fields.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const SOURCE = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/profiles.js"), "utf8");

// ===== 最小 DOM stub（与 test_profiles_token_dialog.js 同模式）=====

function makeElement(initial) {
  const el = {
    value: "", placeholder: "", checked: false, textContent: "",
    className: "", style: {}, dataset: {}, innerHTML: "", onclick: null,
    focus() {}, select() {}, setSelectionRange() {},
  };
  el.classList = {
    contains(c) { return el.className.split(/\s+/).filter(Boolean).includes(c); },
    add(c) { if (!el.classList.contains(c)) el.className = el.className ? `${el.className} ${c}` : c; },
    remove(c) { el.className = el.className.split(/\s+/).filter(x => x && x !== c).join(" "); },
  };
  return Object.assign(el, initial);
}

function createPage() {
  const formControls = [];   // collectGeneralValues/fillGeneralForm 的 [data-key] 查询结果
  const elements = new Map();

  const documentStub = {
    documentElement: { lang: "zh" },
    body: makeElement(),
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: (sel) => sel.includes("[data-key]") ? [...formControls] : [],
    createElement: () => makeElement(),
    addEventListener: () => {},
    execCommand: () => true,
  };

  const toasts = [];
  const sandbox = {
    document: documentStub,
    MutationObserver: class { observe() {} disconnect() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { protocol: "http:", port: "61830", host: "127.0.0.1:61830" },
    navigator: {},
    I18N: new Proxy({}, { get: (_t, key) => String(key) }),
    API: {
      async get() { return []; },
      async post() { return {}; },
      async put() { return {}; },
      async del() { return {}; },
    },
    showModal() {}, hideModal() {},
    confirmDialog: async () => true,
    toast: (msg, type) => toasts.push({ msg, type }),
    withLoading: async (_btn, fn) => fn(),
    escapeHtml: (s) => String(s),
    translateName: (s) => s,
    setupRuleModals() {},
    showLoading() {}, hideLoading() {},
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SOURCE + `
;globalThis.__hooks = {
  get fieldMeta() { return fieldMeta; },
  set fieldMeta(v) { fieldMeta = v; },
  renderField, renderGeneralForm, collectGeneralValues, fillGeneralForm,
};`, sandbox, { filename: "profiles.js" });

  const reg = (id, initial) => {
    const el = makeElement(initial);
    elements.set(id, el);
    return el;
  };
  return { sandbox, formControls, reg, toasts };
}

function control(formControls, key, type, extra) {
  const el = makeElement(Object.assign({ dataset: { key, type } }, extra));
  formControls.push(el);
  return el;
}

// ===== T1 renderField：forced → disabled + data-forced + 徽标（全类型）=====

const FIELD_TYPES = [
  ["bool", { key: "auto-suspend", forced: true }, /<input type="checkbox"[^>]* disabled data-forced="1"/],
  ["select", { key: "block-quic", forced: true, options: [{ value: "per-policy" }] },
   /<select[^>]* disabled data-forced="1"/],
  ["tags", { key: "some-tags", forced: true }, /<textarea[^>]* disabled data-forced="1"/],
  ["int", { key: "compatibility-mode", forced: true }, /<input type="number"[^>]* disabled data-forced="1"/],
  ["text", { key: "some-text", forced: true }, /<input type="text"[^>]* disabled data-forced="1"/],
];

{
  const page = createPage();
  for (const [type, f, disabledRe] of FIELD_TYPES) {
    const html = page.sandbox.__hooks.renderField(
      Object.assign({ type, label_zh: "字段", label_en: "Field", hint_zh: "", hint_en: "" }, f), 7);
    assert.ok(disabledRe.test(html), `${type} forced 字段必须 disabled + data-forced`);
    assert.ok(html.includes("badge-warning"), `${type} forced 字段必须带锁定徽标`);
    assert.ok(html.includes('general.forced_badge'), `${type} forced 徽标使用 I18N 文案`);
  }

  // 非 forced 字段：无任何禁用痕迹
  const plain = page.sandbox.__hooks.renderField(
    { type: "bool", key: "wifi-assist", label_zh: "Wi-Fi", label_en: "Wi-Fi" }, 7);
  assert.ok(!plain.includes("disabled"), "非 forced 字段不得 disabled");
  assert.ok(!plain.includes("data-forced"), "非 forced 字段不得带 data-forced");
  assert.ok(!plain.includes("badge-warning"), "非 forced 字段不得带锁定徽标");
  console.log("T1 renderField：五类型 forced 全禁用 + 徽标，非 forced 干净 通过");
}

// ===== T2 renderGeneralForm：服务端 metadata 驱动整表渲染 =====

{
  const page = createPage();
  page.reg("groups-container-3");
  page.sandbox.__hooks.fieldMeta = [
    { key: "advanced", label_zh: "高级", label_en: "Advanced", fields: [
      { key: "block-quic", type: "select", label_zh: "QUIC", label_en: "QUIC",
        forced: true, options: [{ value: "per-policy" }] },
      { key: "loglevel", type: "select", label_zh: "日志", label_en: "Log",
        options: [{ value: "notify" }] },
    ] },
  ];
  page.sandbox.__hooks.renderGeneralForm(3);
  const html = page.sandbox.document.getElementById("groups-container-3").innerHTML;
  assert.ok(/disabled data-forced="1"/.test(html), "整表渲染 forced 字段必须禁用");
  assert.ok((html.match(/data-forced="1"/g) || []).length === 1, "仅 forced 字段禁用");
  console.log("T2 renderGeneralForm：metadata forced 驱动整表禁用 通过");
}

// ===== T3 collectGeneralValues：forced 键不收集（含 DOM 篡改场景）=====

{
  const page = createPage();
  page.sandbox.__hooks.fieldMeta = [
    { key: "g", label_zh: "", label_en: "", fields: [
      { key: "block-quic", type: "select", forced: true },
      { key: "auto-suspend", type: "bool", forced: true },
      { key: "compatibility-mode", type: "int", forced: true },
      { key: "wifi-assist", type: "bool" },
      { key: "loglevel", type: "select" },
    ] },
  ];
  // DOM 篡改：forced 控件的 disabled/data-forced 全被移除、值改成危险值
  const blockQuic = control(page.formControls, "block-quic", "select", { value: "proxy" });
  const autoSuspend = control(page.formControls, "auto-suspend", "bool", { checked: false });
  const compat = control(page.formControls, "compatibility-mode", "int", { value: "3" });
  const wifi = control(page.formControls, "wifi-assist", "bool", { checked: true });
  const log = control(page.formControls, "loglevel", "select", { value: "info" });
  page.reg("custom-json-5", { value: "" });

  const values = page.sandbox.__hooks.collectGeneralValues(5);
  assert.ok(!("block-quic" in values), "forced select（值被篡改为 proxy）不得收集");
  assert.ok(!("auto-suspend" in values), "forced bool（勾选被篡改）不得收集");
  assert.ok(!("compatibility-mode" in values), "forced int（值被篡改为 3）不得收集");
  assert.equal(values["wifi-assist"], true, "普通 bool 照常收集");
  assert.equal(values["loglevel"], "info", "普通 select 照常收集");

  // 依据是服务端 metadata 而非 DOM：篡改元素属性不影响排除集合
  assert.equal(blockQuic.dataset.forced, undefined);
  assert.equal(autoSuspend.disabled, undefined);

  // 自定义 JSON 入口照常合并（危险强制值由后端 422 拒绝——服务端防线）
  page.sandbox.document.getElementById("custom-json-5").value =
    '{"block-quic": "proxy", "test-timeout": 9}';
  const merged = page.sandbox.__hooks.collectGeneralValues(5);
  assert.equal(merged["test-timeout"], 9, "JSON 普通键照常收集");
  assert.equal(merged["block-quic"], "proxy", "JSON 手工路径保留（后端拒绝危险值）");
  console.log("T3 collectGeneralValues：forced 排除 + DOM 篡改免疫 + JSON 入口保留 通过");
}

// ===== T4 fillGeneralForm：disabled 控件仍展示服务端强制值 =====

{
  const page = createPage();
  const autoSuspend = control(page.formControls, "auto-suspend", "bool", { checked: false });
  const blockQuic = control(page.formControls, "block-quic", "select", { value: "" });
  const wifi = control(page.formControls, "wifi-assist", "bool", { checked: false });

  page.sandbox.__hooks.fillGeneralForm(9, {
    "auto-suspend": true, "block-quic": "per-policy", "wifi-assist": true,
  });
  assert.equal(autoSuspend.checked, true, "disabled checkbox 仍填充强制值");
  assert.equal(blockQuic.value, "per-policy", "disabled select 仍填充强制值");
  assert.equal(wifi.checked, true, "普通 checkbox 照常填充");
  console.log("T4 fillGeneralForm：强制值展示不受禁用影响 通过");
}

console.log("JS 行为测试：forced 字段渲染禁用/徽标 + 收集排除（DOM 篡改免疫）+ 强制值展示 全部通过");
