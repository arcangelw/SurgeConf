/** profiles.js 订阅 Token 对话框行为测试（node vm + DOM stub，非仅语法检查）
 *
 * 覆盖（fix round 1 finding 1）：
 *  - buildSubscribeUrl 纯函数：当前所选地址/协议/端口 + 新路径 + token 逐参数编码
 *  - 首次生成 / 轮换后 token-url-input 赋值（轮换需确认弹窗）
 *  - 按钮（closeTokenDialog）/ ESC 与遮罩（hideModal 直接触发 class 移除，与 app.js 行为一致）
 *    三种关闭路径都立即清空明文
 *  - 复制（execCommand 同步路径 + Clipboard API 异步路径）；空值时提示先生成
 *  - token 不写入全局变量之外的 DOM data 属性 / localStorage
 *
 * 运行：node tests/js/test_profiles_token_dialog.js
 */

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const REPO_ROOT = path.resolve(__dirname, "..", "..");

// ===== 最小 DOM / 浏览器 stub =====

function makeElement(initial) {
  const el = {
    value: "", placeholder: "", checked: false, textContent: "",
    className: "", style: {}, dataset: {}, onclick: null,
    _observers: [],
    focus() {}, select() {}, setSelectionRange() {},
  };
  const classList = {
    contains(c) { return el.className.split(/\s+/).filter(Boolean).includes(c); },
    add(c) {
      if (!classList.contains(c)) {
        el.className = el.className ? `${el.className} ${c}` : c;
        fireObservers(el);
      }
    },
    remove(c) {
      el.className = el.className.split(/\s+/).filter(x => x && x !== c).join(" ");
      fireObservers(el);
    },
  };
  el.classList = classList;
  return Object.assign(el, initial);
}

function fireObservers(el) {
  for (const entry of el._observers) {
    if (entry.alive && (entry.opts.attributeFilter || []).includes("class")) {
      entry.cb();
    }
  }
}

class MutationObserverStub {
  constructor(cb) { this.cb = cb; this._entry = null; }
  observe(target, opts) {
    this._entry = { cb: this.cb, opts, alive: true };
    target._observers.push(this._entry);
  }
  disconnect() { if (this._entry) this._entry.alive = false; }
}

const elements = new Map();
function reg(id, initial) { const el = makeElement(initial); elements.set(id, el); return el; }

const storageCalls = [];
const execCalls = [];
const clipboardWrites = [];
const toasts = [];
let confirmCalls = 0;

const documentStub = {
  documentElement: { lang: "zh" },
  body: makeElement(),
  getElementById: (id) => elements.get(id) || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeElement(),
  addEventListener: () => {},
  execCommand: (cmd) => { execCalls.push(cmd); return documentStub._execResult; },
  _execResult: true,
};

// 与 app.js 真实行为一致：show/hide 仅操作 class（从而触发 MutationObserver）
function showModal(id) { documentStub.getElementById(id)?.classList.add("show"); }
function hideModal(id) { documentStub.getElementById(id)?.classList.remove("show"); }

const state = { token: "tok-first" };

const API = {
  async get(url) { return url.includes("server-ips") ? { ips: [] } : []; },
  async post(url) { return { profile_id: 1, token: state.token }; },
  async put() { return {}; },
  async del() { return {}; },
};

const sandbox = {
  document: documentStub,
  MutationObserver: MutationObserverStub,
  localStorage: {
    getItem: () => null,
    setItem: (k, v) => storageCalls.push([k, v]),
    removeItem: () => {},
  },
  location: { protocol: "http:", port: "61830", host: "127.0.0.1:61830" },
  navigator: { clipboard: { writeText: async (t) => clipboardWrites.push(t) } },
  I18N: new Proxy({}, { get: (_t, key) => String(key) }),
  API,
  showModal,
  hideModal,
  confirmDialog: async () => { confirmCalls += 1; return true; },
  toast: (msg, type) => toasts.push({ msg, type }),
  withLoading: async (_btn, fn) => fn(),
  escapeHtml: (s) => String(s),
  translateName: (s) => s,
  setupRuleModals: () => {},
  showLoading: () => {},
  hideLoading: () => {},
  console,
};

vm.createContext(sandbox);

const source = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/profiles.js"), "utf8");
// 测试钩子：模块级 let（allProfiles）与目标函数导出到 context 全局
vm.runInContext(source + `
;globalThis.__hooks = {
  get allProfiles() { return allProfiles; },
  set allProfiles(v) { allProfiles = v; },
  buildSubscribeUrl, generateProfileToken, showTokenDialog,
  closeTokenDialog, copyTokenUrl, renderTokenStatus,
};`, sandbox, { filename: "profiles.js" });

const hooks = sandbox.__hooks;

(async () => {
  // 排空 init()/loadData() 的全部异步步骤（避免空列表赋值晚到覆盖测试数据）
  await new Promise((resolve) => setTimeout(resolve, 20));


  // 页面元素：生成 Tab + token 对话框
  reg("profile-list");
  const ipSelect = reg("ip-select-1", { value: "192.168.1.9" });
  reg("token-status-1");
  const btn = reg("token-btn-1");
  const modal = reg("token-modal");
  const input = reg("token-url-input");

  const urlOf = (token) =>
    `http://192.168.1.9:61830/api/configs/profiles/1/subscribe?token=${encodeURIComponent(token)}`;

  // ---- T1 buildSubscribeUrl 纯函数：逐参数安全编码 ----
  assert.equal(hooks.buildSubscribeUrl(1, "abc/DEF+x="), urlOf("abc/DEF+x="));
  assert.equal(hooks.buildSubscribeUrl(1, "plain"), urlOf("plain"));
  // 未注册 ip-select 的档案回退 127.0.0.1
  assert.equal(
    hooks.buildSubscribeUrl(2, "t"),
    "http://127.0.0.1:61830/api/configs/profiles/2/subscribe?token=t");
  // 切换所选地址后重算
  ipSelect.value = "10.0.0.8";
  assert.equal(
    hooks.buildSubscribeUrl(1, "t"),
    "http://10.0.0.8:61830/api/configs/profiles/1/subscribe?token=t");
  ipSelect.value = "192.168.1.9";

  // ---- T2 首次生成：无确认弹窗，对话框显示完整 URL ----
  hooks.allProfiles = [{ id: 1, name: "默认配置", is_default: true, locale: "zh",
                         description: null, token_configured: false }];
  state.token = "tok-first";
  await hooks.generateProfileToken(1, btn);
  assert.equal(confirmCalls, 0, "首次生成不应弹轮换确认");
  assert.ok(modal.classList.contains("show"), "对话框应打开");
  assert.equal(input.value, urlOf("tok-first"));
  assert.equal(hooks.allProfiles[0].token_configured, true, "UI 状态应更新");
  assert.ok(elements.get("token-status-1").className.includes("badge-success"));

  // ---- T3 轮换：确认弹窗一次，新 token 覆盖旧明文 ----
  state.token = "tok-rotated?2";
  await hooks.generateProfileToken(1, btn);
  assert.equal(confirmCalls, 1, "轮换应弹一次确认");
  assert.equal(input.value, urlOf("tok-rotated?2"));
  assert.ok(!input.value.includes("tok-first"), "旧明文不得残留");

  // ---- T3b 复制：execCommand 同步路径 ----
  const execBefore = execCalls.length;
  hooks.copyTokenUrl();
  assert.ok(execCalls.slice(execBefore).includes("copy"), "应调用 execCommand copy");
  assert.equal(toasts[toasts.length - 1].msg, "generator.copied");

  // ---- T3c 复制：Clipboard API 异步回退路径 ----
  documentStub._execResult = false;
  hooks.showTokenDialog(1, "tok-clip");
  hooks.copyTokenUrl();
  await Promise.resolve();
  assert.ok(clipboardWrites.includes(urlOf("tok-clip")), "clipboard 应收到完整 URL");
  documentStub._execResult = true;

  // ---- T4 按钮关闭：明文立即清空 ----
  hooks.showTokenDialog(1, "tok-btn-close");
  assert.notEqual(input.value, "");
  hooks.closeTokenDialog();
  assert.ok(!modal.classList.contains("show"));
  assert.equal(input.value, "", "按钮关闭后明文必须清空");

  // ---- T5 ESC / 遮罩路径：hideModal 直接移除 class（app.js 行为），明文清空 ----
  hooks.showTokenDialog(1, "tok-esc");
  assert.notEqual(input.value, "");
  hideModal("token-modal");
  assert.equal(input.value, "", "ESC/遮罩关闭后明文必须清空");

  // ---- T6 空值复制：提示先生成，不调用复制 ----
  const execBefore2 = execCalls.length;
  hooks.copyTokenUrl();
  assert.equal(execCalls.length, execBefore2, "无 token 时不得复制");
  assert.equal(toasts[toasts.length - 1].msg, "profile.token_copy_first");
  assert.equal(toasts[toasts.length - 1].type, "error");

  // ---- T7 token 不落 DOM data 属性 / localStorage / 全局缓存 ----
  for (const [id, el] of elements) {
    assert.deepEqual(el.dataset, {}, `元素 ${id} 不得以 data 属性缓存 token`);
    assert.ok(!String(el.className).includes("tok-"), `元素 ${id} class 不得携带 token`);
  }
  assert.equal(storageCalls.length, 0, "不得写 localStorage");
  assert.equal(input.value, "", "对话框外不得保留明文");

  console.log(`JS 行为测试：7 组断言全部通过（生成/轮换/三路关闭/复制/编码/无残留）`);
})().catch((e) => { console.error(e); process.exit(1); });
