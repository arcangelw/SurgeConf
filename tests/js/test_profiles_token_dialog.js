/** profiles.js 订阅 Token 对话框行为测试（node vm + DOM stub，非仅语法检查）
 *
 * 覆盖（fix round 1 finding 1 + fix round 2 端口/host 修正）：
 *  - buildSubscribeUrl 纯函数：当前所选地址/协议/端口 + 新路径 + token 逐参数编码
 *  - 端口省略结合协议：仅 (http,80)/(https,443) 省略；http:443 / https:80 / 自定义端口保留
 *  - IPv6 字面量以 [..] 包裹；hostname 编码不退化
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
const SOURCE = fs.readFileSync(path.join(REPO_ROOT, "app/static/js/profiles.js"), "utf8");

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

/** 按指定协议/端口加载一份 profiles.js（独立 context + 独立 DOM） */
function createPage(protocol, port) {
  const elements = new Map();
  const execCalls = [];
  const clipboardWrites = [];
  const toasts = [];
  const storageCalls = [];
  const confirm = { calls: 0 };
  const state = { token: "tok-first", execResult: true };

  const documentStub = {
    documentElement: { lang: "zh" },
    body: makeElement(),
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => makeElement(),
    addEventListener: () => {},
    execCommand: (cmd) => { execCalls.push(cmd); return state.execResult; },
  };

  // 与 app.js 真实行为一致：show/hide 仅操作 class（从而触发 MutationObserver）
  const showModal = (id) => documentStub.getElementById(id)?.classList.add("show");
  const hideModal = (id) => documentStub.getElementById(id)?.classList.remove("show");

  const API = {
    async get(url) { return url.includes("server-ips") ? { ips: [] } : []; },
    async post() { return { profile_id: 1, token: state.token }; },
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
    location: { protocol, port, host: `127.0.0.1:${port}` },
    navigator: { clipboard: { writeText: async (t) => clipboardWrites.push(t) } },
    I18N: new Proxy({}, { get: (_t, key) => String(key) }),
    API,
    showModal,
    hideModal,
    confirmDialog: async () => { confirm.calls += 1; return true; },
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
  vm.runInContext(SOURCE + `
;globalThis.__hooks = {
  get allProfiles() { return allProfiles; },
  set allProfiles(v) { allProfiles = v; },
  buildSubscribeUrl, generateProfileToken, showTokenDialog,
  closeTokenDialog, copyTokenUrl, renderTokenStatus,
};`, sandbox, { filename: "profiles.js" });

  const reg = (id, initial) => {
    const el = makeElement(initial);
    elements.set(id, el);
    return el;
  };
  return { sandbox, elements, reg, documentStub, execCalls, clipboardWrites,
           toasts, storageCalls, confirm, state, showModal, hideModal };
}

(async () => {
  // ---- T1 端口矩阵：省略必须结合协议，交叉组合保留 ----
  const matrix = [
    ["http:", "80", ""],
    ["https:", "443", ""],
    ["http:", "443", ":443"],
    ["https:", "80", ":80"],
    ["https:", "8443", ":8443"],
  ];
  for (const [protocol, port, suffix] of matrix) {
    const page = createPage(protocol, port);
    page.reg("ip-select-7", { value: "198.51.100.7" });
    assert.equal(
      page.sandbox.__hooks.buildSubscribeUrl(7, "tk"),
      `${protocol}//198.51.100.7${suffix}/api/configs/profiles/7/subscribe?token=tk`,
      `${protocol}//...:${port} 组合端口处理错误`);
    await new Promise((r) => setTimeout(r, 0)); // 排空该 context 的 init 微任务
  }

  // 主页面：http + 自定义端口 61830
  const page = createPage("http:", "61830");
  await new Promise((resolve) => setTimeout(resolve, 20)); // 排空 init()/loadData()

  page.reg("profile-list");
  const ipSelect = page.reg("ip-select-1", { value: "203.0.113.9" });
  page.reg("token-status-1");
  const btn = page.reg("token-btn-1");
  const modal = page.reg("token-modal");
  const input = page.reg("token-url-input");
  const hooks = page.sandbox.__hooks;

  const urlOf = (token) =>
    `http://203.0.113.9:61830/api/configs/profiles/1/subscribe?token=${encodeURIComponent(token)}`;

  // ---- T1b 纯函数：逐参数安全编码 / 回退地址 / IPv6 / hostname ----
  assert.equal(hooks.buildSubscribeUrl(1, "abc/DEF+x="), urlOf("abc/DEF+x="));
  assert.equal(
    hooks.buildSubscribeUrl(2, "t"),
    "http://127.0.0.1:61830/api/configs/profiles/2/subscribe?token=t");
  ipSelect.value = "198.51.100.8";
  assert.equal(
    hooks.buildSubscribeUrl(1, "t"),
    "http://198.51.100.8:61830/api/configs/profiles/1/subscribe?token=t");
  // IPv6 字面量：方括号包裹，冒号不被百分号编码
  ipSelect.value = "fe80::1";
  assert.equal(
    hooks.buildSubscribeUrl(1, "t"),
    "http://[fe80::1]:61830/api/configs/profiles/1/subscribe?token=t");
  // hostname：大小写/连字符/下划线/点不被改写（编码不退化）
  ipSelect.value = "My.Host-Name_1.example";
  assert.equal(
    hooks.buildSubscribeUrl(1, "t"),
    "http://My.Host-Name_1.example:61830/api/configs/profiles/1/subscribe?token=t");
  ipSelect.value = "203.0.113.9";

  // ---- T2 首次生成：无确认弹窗，对话框显示完整 URL ----
  hooks.allProfiles = [{ id: 1, name: "默认配置", is_default: true, locale: "zh",
                         description: null, token_configured: false }];
  page.state.token = "tok-first";
  await hooks.generateProfileToken(1, btn);
  assert.equal(page.confirm.calls, 0, "首次生成不应弹轮换确认");
  assert.ok(modal.classList.contains("show"), "对话框应打开");
  assert.equal(input.value, urlOf("tok-first"));
  assert.equal(hooks.allProfiles[0].token_configured, true, "UI 状态应更新");
  assert.ok(page.elements.get("token-status-1").className.includes("badge-success"));

  // ---- T3 轮换：确认弹窗一次，新 token 覆盖旧明文 ----
  page.state.token = "tok-rotated?2";
  await hooks.generateProfileToken(1, btn);
  assert.equal(page.confirm.calls, 1, "轮换应弹一次确认");
  assert.equal(input.value, urlOf("tok-rotated?2"));
  assert.ok(!input.value.includes("tok-first"), "旧明文不得残留");

  // ---- T3b 复制：execCommand 同步路径 ----
  const execBefore = page.execCalls.length;
  hooks.copyTokenUrl();
  assert.ok(page.execCalls.slice(execBefore).includes("copy"), "应调用 execCommand copy");
  assert.equal(page.toasts[page.toasts.length - 1].msg, "generator.copied");

  // ---- T3c 复制：Clipboard API 异步回退路径 ----
  page.state.execResult = false;
  hooks.showTokenDialog(1, "tok-clip");
  hooks.copyTokenUrl();
  await Promise.resolve();
  assert.ok(page.clipboardWrites.includes(urlOf("tok-clip")), "clipboard 应收到完整 URL");
  page.state.execResult = true;

  // ---- T4 按钮关闭：明文立即清空 ----
  hooks.showTokenDialog(1, "tok-btn-close");
  assert.notEqual(input.value, "");
  hooks.closeTokenDialog();
  assert.ok(!modal.classList.contains("show"));
  assert.equal(input.value, "", "按钮关闭后明文必须清空");

  // ---- T5 ESC / 遮罩路径：hideModal 直接移除 class（app.js 行为），明文清空 ----
  hooks.showTokenDialog(1, "tok-esc");
  assert.notEqual(input.value, "");
  page.hideModal("token-modal");
  assert.equal(input.value, "", "ESC/遮罩关闭后明文必须清空");

  // ---- T6 空值复制：提示先生成，不调用复制 ----
  const execBefore2 = page.execCalls.length;
  hooks.copyTokenUrl();
  assert.equal(page.execCalls.length, execBefore2, "无 token 时不得复制");
  assert.equal(page.toasts[page.toasts.length - 1].msg, "profile.token_copy_first");
  assert.equal(page.toasts[page.toasts.length - 1].type, "error");

  // ---- T7 token 不落 DOM data 属性 / localStorage / 全局缓存 ----
  for (const [id, el] of page.elements) {
    assert.deepEqual(el.dataset, {}, `元素 ${id} 不得以 data 属性缓存 token`);
    assert.ok(!String(el.className).includes("tok-"), `元素 ${id} class 不得携带 token`);
  }
  assert.equal(page.storageCalls.length, 0, "不得写 localStorage");
  assert.equal(input.value, "", "对话框外不得保留明文");

  console.log("JS 行为测试：端口矩阵 5 组 + 生成/轮换/三路关闭/复制/编码/IPv6/无残留 全部通过");
})().catch((e) => { console.error(e); process.exit(1); });
