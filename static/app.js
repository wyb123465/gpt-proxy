const providerList = document.querySelector("#providerList");
const providerTemplate = document.querySelector("#providerTemplate");
const defaultModelInput = document.querySelector("#defaultModel");
const saveMessage = document.querySelector("#saveMessage");
const channelOverviewBody = document.querySelector("#channelOverviewBody");
const healthBadge = document.querySelector("#healthBadge");
const usableCount = document.querySelector("#usableCount");
const totalCalls = document.querySelector("#totalCalls");
const modelSummary = document.querySelector("#modelSummary");
const requestLogBody = document.querySelector("#requestLogBody");
const overviewProviderList = document.querySelector("#overviewProviderList");
const overviewProtocolList = document.querySelector("#overviewProtocolList");
const overviewRequestList = document.querySelector("#overviewRequestList");
const checkReport = document.querySelector("#checkReport");
const proxyTokenState = document.querySelector("#proxyTokenState");
const encryptionState = document.querySelector("#encryptionState");
const rateLimitState = document.querySelector("#rateLimitState");
const bodyLimitState = document.querySelector("#bodyLimitState");
const cooldownState = document.querySelector("#cooldownState");
const importConfigFile = document.querySelector("#importConfigFile");
const localProxyTokenInput = document.querySelector("#localProxyToken");
const saveAndTestBtn = document.querySelector("#saveAndTestBtn");
const viewKicker = document.querySelector("#viewKicker");
const viewTitle = document.querySelector("#viewTitle");
const viewDescription = document.querySelector("#viewDescription");
const requestStatusFilter = document.querySelector("#requestStatusFilter");
const requestSearchInput = document.querySelector("#requestSearchInput");
const viewButtons = Array.from(document.querySelectorAll("[data-view-target]"));
const viewPanels = Array.from(document.querySelectorAll("[data-view-panel]"));
const viewJumpButtons = Array.from(document.querySelectorAll("[data-view-jump]"));

let providers = [];
let latestRequests = [];
localProxyTokenInput.value = localStorage.getItem("gpt_proxy_access_token") || "";

const protocolLabels = {
  openai: "OpenAI",
  domestic: "国内",
  claude: "Claude",
  gemini: "Gemini",
};

const viewCopy = {
  overview: {
    kicker: "Overview",
    title: "总览",
    description: "查看本地代理状态、当前 API 和最近请求。",
  },
  providers: {
    kicker: "Providers",
    title: "API 配置",
    description: "维护 provider、优先级、多 key 轮询、模型别名和连通性测试。",
  },
  usage: {
    kicker: "Client setup",
    title: "调用方式",
    description: "复制统一 Base URL，并把其他客户端指向这个本地代理。",
  },
  requests: {
    kicker: "Request logs",
    title: "请求日志",
    description: "查看最近请求的 provider、状态码、耗时、回退次数和流式状态。",
  },
  security: {
    kicker: "Security",
    title: "安全设置",
    description: "查看本地访问 token、配置加密、限流和请求体限制。",
  },
};

function setMessage(text, type = "") {
  saveMessage.textContent = text;
  saveMessage.className = `save-message ${type}`.trim();
}

function providerDomId(index) {
  return `provider-card-${index}`;
}

function setActiveView(viewName, updateHash = true) {
  const knownView = viewButtons.some((button) => button.dataset.viewTarget === viewName);
  const nextView = knownView ? viewName : "overview";
  const copy = viewCopy[nextView];

  viewButtons.forEach((button) => {
    const active = button.dataset.viewTarget === nextView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });

  viewPanels.forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.viewPanel === nextView);
  });

  viewKicker.textContent = copy.kicker;
  viewTitle.textContent = copy.title;
  viewDescription.textContent = copy.description;

  if (updateHash) {
    history.replaceState(null, "", `#${nextView}`);
  }
}

function providerMeta(provider) {
  const enabled = provider.enabled === false ? "已停用" : "已启用";
  const keyState = provider.has_api_key || provider.api_key || provider.api_keys_text ? `密钥 ${provider.key_count || 1} 个` : "未设置密钥";
  const calls = Number(provider.calls || 0).toLocaleString("zh-CN");
  const remaining = provider.last_remaining ?? "未知";
  return `${enabled} · ${keyState} · 调用 ${calls} 次 · 剩余额度 ${remaining}`;
}

function providerChips(provider) {
  const protocol = provider.protocol || "openai";
  const chips = [
    {
      text: provider.enabled === false ? "已停用" : "启用中",
      tone: provider.enabled === false ? "warn" : "ok",
    },
    {
      text: protocolLabels[protocol] || protocol,
      tone: "info",
    },
    {
      text: provider.has_api_key || provider.api_key || provider.api_keys_text ? `${provider.key_count || 1} key` : "缺少 key",
      tone: provider.has_api_key || provider.api_key || provider.api_keys_text ? "ok" : "error",
    },
    { text: `优先级 ${provider.priority ?? 0}`, tone: "" },
  ];
  if (provider.use_curl) chips.push({ text: "curl 传输", tone: "warn" });
  if (provider.model) chips.push({ text: provider.model, tone: "" });
  return chips;
}

function renderProviderChips(node, provider) {
  let chipWrap = node.querySelector(".provider-chips");
  if (!chipWrap) {
    chipWrap = document.createElement("div");
    chipWrap.className = "provider-chips";
    node.querySelector(".provider-meta").after(chipWrap);
  }
  chipWrap.innerHTML = "";
  providerChips(provider).forEach((chip) => {
    const item = document.createElement("span");
    item.className = `provider-chip ${chip.tone}`.trim();
    item.textContent = chip.text;
    chipWrap.appendChild(item);
  });
}

function renderOverviewProviders() {
  overviewProviderList.innerHTML = "";
  const sortedProviders = [...providers]
    .sort((left, right) => Number(left.priority || 0) - Number(right.priority || 0))
    .slice(0, 4);

  if (sortedProviders.length === 0) {
    overviewProviderList.innerHTML = '<p class="mini-empty">暂无 provider，先去“API 配置”添加一个。</p>';
    return;
  }

  sortedProviders.forEach((provider) => {
    const item = document.createElement("div");
    item.className = "mini-item";

    const title = document.createElement("strong");
    title.textContent = provider.name || "未命名 API";

    const meta = document.createElement("span");
    const keyState = provider.has_api_key || provider.api_key || provider.api_keys_text ? `${provider.key_count || 1} key` : "缺少 key";
    meta.textContent = `${provider.enabled === false ? "停用" : "启用"} · ${keyState} · 优先级 ${provider.priority ?? 0}`;

    const dot = document.createElement("i");
    dot.className = provider.enabled === false ? "status-dot warn" : keyState === "缺少 key" ? "status-dot error" : "status-dot ok";

    item.append(dot, title, meta);
    overviewProviderList.appendChild(item);
  });
}

function fallbackProtocolCounts() {
  return providers.reduce((counts, provider) => {
    const protocol = provider.protocol || "openai";
    if (!counts[protocol]) counts[protocol] = 0;
    if (provider.enabled !== false) counts[protocol] += 1;
    return counts;
  }, { openai: 0, domestic: 0, claude: 0, gemini: 0 });
}

function renderOverviewProtocols(protocols = null) {
  const counts = protocols || fallbackProtocolCounts();
  overviewProtocolList.innerHTML = "";

  Object.entries(protocolLabels).forEach(([protocol, label]) => {
    const item = document.createElement("div");
    item.className = "protocol-item";

    const name = document.createElement("span");
    name.textContent = label;

    const count = document.createElement("strong");
    count.textContent = Number(counts[protocol] || 0).toLocaleString("zh-CN");

    const hint = document.createElement("small");
    hint.textContent = Number(counts[protocol] || 0) > 0 ? "已配置" : "未配置";

    item.append(name, count, hint);
    overviewProtocolList.appendChild(item);
  });
}

function renderSecurity(security = {}) {
  proxyTokenState.textContent = security.proxy_access_token_enabled ? "已启用" : "未启用";
  encryptionState.textContent = security.config_encryption_enabled ? "已启用" : "未启用";
  rateLimitState.textContent = security.rate_limit_per_minute > 0 ? `${security.rate_limit_per_minute}/分钟` : "未启用";
  bodyLimitState.textContent = security.max_request_bytes ? `${Math.round(security.max_request_bytes / 1024)} KB` : "未限制";
  cooldownState.textContent = security.key_cooldown_seconds ? `${security.key_cooldown_seconds} 秒` : "未启用";
}

function appendChannelCell(row, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text;
  row.appendChild(cell);
}

function renderChannelOverview() {
  channelOverviewBody.innerHTML = "";
  if (providers.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.className = "empty-cell";
    cell.textContent = "暂无 API，点击“添加 API”开始配置。";
    row.appendChild(cell);
    channelOverviewBody.appendChild(row);
    return;
  }

  providers.forEach((provider, index) => {
    const row = document.createElement("tr");
    const protocol = provider.protocol || "openai";
    const hasKey = provider.has_api_key || provider.api_key || provider.api_keys_text;
    const enabled = provider.enabled !== false;

    appendChannelCell(row, provider.name || "新 API");
    appendChannelCell(row, protocolLabels[protocol] || protocol);
    appendChannelCell(row, provider.model || defaultModelInput.value || "-");
    appendChannelCell(row, String(provider.priority ?? 0));
    appendChannelCell(row, hasKey ? `${provider.key_count || 1}` : "0", hasKey ? "" : "error-text");
    appendChannelCell(row, Number(provider.calls || 0).toLocaleString("zh-CN"));
    appendChannelCell(row, enabled ? "启用" : "停用", enabled ? "ok-text" : "error-text");

    const actionCell = document.createElement("td");
    const button = document.createElement("button");
    button.className = "ghost-link";
    button.type = "button";
    button.textContent = "编辑";
    button.addEventListener("click", () => {
      document.querySelector(`#${providerDomId(index)}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    actionCell.appendChild(button);
    row.appendChild(actionCell);

    channelOverviewBody.appendChild(row);
  });
}

function renderProviders() {
  providerList.innerHTML = "";
  providers.forEach((provider, index) => {
    if (provider.enabled === undefined) provider.enabled = true;

    const node = providerTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.index = index;
    node.id = providerDomId(index);
    node.querySelector(".provider-title").textContent = provider.name || "新 API";
    node.querySelector(".provider-meta").textContent = providerMeta(provider);
    renderProviderChips(node, provider);

    for (const input of node.querySelectorAll("[data-field]")) {
      const field = input.dataset.field;
      if (field === "api_keys_text") {
        input.value = provider.api_keys_text || "";
      } else if (input.type === "checkbox") {
        input.checked = Boolean(provider[field]);
      } else if (input.tagName === "SELECT") {
        input.value = provider[field] || "openai";
      } else {
        input.value = provider[field] ?? "";
      }
      input.addEventListener("input", () => {
        if (field === "api_keys_text") {
          providers[index][field] = input.value;
          providers[index].key_count = input.value.split(/\r?\n/).filter((key) => key.trim()).length || provider.key_count || 0;
        } else if (input.type === "checkbox") {
          providers[index][field] = input.checked;
        } else if (field === "priority") {
          providers[index][field] = Number(input.value || 0);
        } else {
          providers[index][field] = input.value;
        }
        node.querySelector(".provider-title").textContent = providers[index].name || "新 API";
        node.querySelector(".provider-meta").textContent = providerMeta(providers[index]);
        renderProviderChips(node, providers[index]);
        renderChannelOverview();
        refreshSummary();
      });
    }

    node.querySelector(".remove-provider").addEventListener("click", () => {
      providers.splice(index, 1);
      renderProviders();
      refreshSummary();
    });

    node.querySelector(".test-provider").addEventListener("click", () => {
      testProvider(index, node);
    });

    node.querySelector(".fetch-models").addEventListener("click", () => {
      fetchModels(index, node);
    });

    const aliasList = node.querySelector(".alias-list");
    const addAliasBtn = node.querySelector(".add-alias");
    renderAliases(aliasList, index);
    addAliasBtn.addEventListener("click", () => {
      if (!providers[index].model_aliases) providers[index].model_aliases = {};
      const key = `gpt-${Object.keys(providers[index].model_aliases).length + 1}`;
      providers[index].model_aliases[key] = providers[index].model || "";
      renderAliases(aliasList, index);
    });

    providerList.appendChild(node);
  });
  renderChannelOverview();
  refreshSummary();
}

function refreshSummary() {
  usableCount.textContent = providers.filter((provider) => provider.enabled !== false && (provider.has_api_key || provider.api_key || provider.api_keys_text)).length;
  totalCalls.textContent = providers
    .reduce((total, provider) => total + Number(provider.calls || 0), 0)
    .toLocaleString("zh-CN");
  modelSummary.textContent = defaultModelInput.value || "未设置";
  renderOverviewProviders();
  renderOverviewProtocols();
}

function authHeaders() {
  const token = localStorage.getItem("gpt_proxy_access_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function withAuth(options = {}) {
  return {
    ...options,
    headers: {
      ...authHeaders(),
      ...(options.headers || {}),
    },
  };
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, withAuth(options));
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("需要本地代理访问密钥：请在“安全与兼容”里填写后刷新");
    }
    throw new Error(data.detail || `请求失败：${response.status}`);
  }
  return data;
}

async function loadConfig() {
  setMessage("正在读取配置...");
  const [health, detailedHealth, config] = await Promise.all([
    fetchJson("/health").catch(() => null),
    fetchJson("/health/detailed").catch(() => null),
    fetchJson("/api/config"),
  ]);

  healthBadge.textContent = health ? "服务正常" : "服务异常";
  healthBadge.className = health ? "badge" : "badge muted";
  defaultModelInput.value = config.default_model || "gpt-4o";
  providers = config.providers || [];
  renderSecurity(config.security);
  renderProviders();
  renderOverviewProtocols(detailedHealth?.protocols);
  await loadRequests();
  setMessage("配置已载入", "ok");
}

function providerKeys(provider) {
  return String(provider.api_keys_text || "")
    .split(/\r?\n/)
    .map((key) => key.trim())
    .filter(Boolean);
}

function collectPayload() {
  return {
    default_model: defaultModelInput.value.trim() || "gpt-4o",
    providers: providers.map((provider) => ({
      name: String(provider.name || "").trim(),
      protocol: String(provider.protocol || "openai").trim() || "openai",
      base_url: String(provider.base_url || "").trim(),
      model: String(provider.model || "").trim(),
      priority: Number(provider.priority || 0),
      api_key: "",
      api_keys: providerKeys(provider),
      api_key_env: String(provider.api_key_env || "").trim(),
      enabled: provider.enabled !== false,
      use_curl: Boolean(provider.use_curl),
      model_aliases: provider.model_aliases && typeof provider.model_aliases === "object"
        ? Object.fromEntries(
            Object.entries(provider.model_aliases)
              .filter(([key, value]) => key.trim() && value.trim())
              .map(([key, value]) => [key.trim(), value.trim()])
          )
        : {},
    })),
  };
}

async function saveConfig() {
  setMessage("正在保存...");
  try {
    const config = await fetchJson("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectPayload()),
    });
    defaultModelInput.value = config.default_model;
    providers = config.providers || [];
    renderSecurity(config.security);
    renderProviders();
    setMessage("已保存，下一次请求会使用新配置", "ok");
    return config;
  } catch (error) {
    setMessage(error.message, "error");
    throw error;
  }
}

function renderCheckReport(report) {
  checkReport.hidden = false;
  checkReport.innerHTML = "";

  const header = document.createElement("div");
  header.className = "check-report-head";
  const title = document.createElement("strong");
  title.textContent = `检测完成：${report.ok}/${report.total} 个可用`;
  const summary = document.createElement("span");
  summary.textContent = report.failed ? `${report.failed} 个失败，需要检查 key、模型或 Base URL` : "全部 provider 响应正常";
  header.append(title, summary);
  checkReport.appendChild(header);

  const list = document.createElement("div");
  list.className = "check-report-list";
  (report.results || []).forEach((result) => {
    const item = document.createElement("div");
    item.className = `check-report-item ${result.ok ? "ok" : "error"}`;

    const name = document.createElement("strong");
    name.textContent = result.provider || "-";

    const meta = document.createElement("span");
    const protocol = protocolLabels[result.protocol] || result.protocol || "OpenAI";
    meta.textContent = `${protocol} · ${result.model || "-"} · ${result.status}`;

    const detail = document.createElement("small");
    detail.textContent = result.ok ? "连接正常" : formatCheckDetail(result.detail);

    item.append(name, meta, detail);
    list.appendChild(item);
  });
  checkReport.appendChild(list);
}

async function saveAndTestAll() {
  saveAndTestBtn.disabled = true;
  saveAndTestBtn.textContent = "测试中";
  try {
    await saveConfig();
    setMessage("正在批量检测所有可用 API...");
    const report = await fetchJson("/api/providers/check-all", { method: "POST" });
    renderCheckReport(report);
    if (report.total === 0) {
      setMessage("没有可测试的 API：请先添加并启用 provider", "error");
      return;
    }

    await loadConfig();
    setMessage(`检测完成：${report.ok}/${report.total} 个 API 可用`, report.ok ? "ok" : "error");
  } catch (error) {
    setMessage(error.message, "error");
  } finally {
    saveAndTestBtn.disabled = false;
    saveAndTestBtn.textContent = "保存并测试全部";
  }
}

async function exportConfig() {
  try {
    const config = await fetchJson("/api/config/export");
    const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `gpt-proxy-config-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(url);
    setMessage("配置已导出；请妥善保存，里面包含真实密钥", "ok");
  } catch (error) {
    setMessage(error.message, "error");
  }
}

async function importConfig(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const payload = JSON.parse(text);
    const config = await fetchJson("/api/config/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    defaultModelInput.value = config.default_model;
    providers = config.providers || [];
    renderSecurity(config.security);
    renderProviders();
    setMessage("配置已导入并保存", "ok");
  } catch (error) {
    setMessage(`导入失败：${error.message}`, "error");
  } finally {
    importConfigFile.value = "";
  }
}

function formatCheckDetail(detail) {
  if (!detail) return "无详细信息";
  if (typeof detail === "string") return detail;
  if (detail.error?.message) return detail.error.message;
  if (detail.detail) return typeof detail.detail === "string" ? detail.detail : JSON.stringify(detail.detail);
  return JSON.stringify(detail);
}

async function testProvider(index, node) {
  const provider = providers[index];
  if (!provider?.name) {
    setMessage("请先填写 API 名称并保存", "error");
    return;
  }

  const button = node.querySelector(".test-provider");
  const meta = node.querySelector(".provider-meta");
  button.disabled = true;
  button.textContent = "测试中";
  meta.textContent = "正在请求该 API...";

  try {
    const result = await fetchJson(`/api/providers/${encodeURIComponent(provider.name)}/check`, {
      method: "POST",
    });
    if (result.status === "no_api_key") {
      meta.textContent = `${providerMeta(provider)} · 请先填写 API Key 并保存`;
      meta.className = "provider-meta error";
      setMessage(`${provider.name} 还没有可用密钥`, "error");
      return;
    }
    meta.textContent = result.ok
      ? `${providerMeta(provider)} · 测试通过`
      : `${providerMeta(provider)} · 测试失败：${formatCheckDetail(result.detail)}`;
    meta.className = result.ok ? "provider-meta ok" : "provider-meta error";
    setMessage(result.ok ? `${provider.name} 连接正常` : `${provider.name} 连接失败`, result.ok ? "ok" : "error");
  } catch (error) {
    meta.textContent = `${providerMeta(provider)} · 测试失败：${error.message}`;
    meta.className = "provider-meta error";
    setMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "测试";
  }
}

function renderAliases(aliasList, index) {
  aliasList.innerHTML = "";
  const aliases = providers[index].model_aliases || {};
  const entries = Object.entries(aliases);
  if (entries.length === 0) {
    aliasList.innerHTML = '<div class="model-empty">暂无别名</div>';
    return;
  }
  entries.forEach(([from, to]) => {
    const row = document.createElement("div");
    row.className = "alias-row";
    const fromInput = document.createElement("input");
    fromInput.placeholder = "请求的模型名";
    fromInput.value = from;
    const arrow = document.createElement("span");
    arrow.className = "alias-arrow";
    arrow.textContent = "→";
    const toInput = document.createElement("input");
    toInput.placeholder = "实际发送";
    toInput.value = to;
    const removeBtn = document.createElement("button");
    removeBtn.className = "ghost-btn remove-alias";
    removeBtn.type = "button";
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => {
      delete providers[index].model_aliases[from];
      renderAliases(aliasList, index);
    });
    fromInput.addEventListener("input", () => {
      const oldKey = from;
      const newKey = fromInput.value.trim();
      if (newKey && newKey !== oldKey) {
        const value = providers[index].model_aliases[oldKey];
        delete providers[index].model_aliases[oldKey];
        providers[index].model_aliases[newKey] = value;
      }
    });
    toInput.addEventListener("input", () => {
      const key = fromInput.value.trim();
      if (key) providers[index].model_aliases[key] = toInput.value.trim();
    });
    row.append(fromInput, arrow, toInput, removeBtn);
    aliasList.appendChild(row);
  });
}

async function fetchModels(index, node) {
  const provider = providers[index];
  if (!provider?.name) {
    setMessage("请先填写 API 名称并保存", "error");
    return;
  }

  const button = node.querySelector(".fetch-models");
  const dropdown = node.querySelector(".model-dropdown");
  const modelInput = node.querySelector('[data-field="model"]');
  button.disabled = true;
  button.textContent = "获取中";
  dropdown.hidden = true;
  dropdown.innerHTML = "";

  try {
    const data = await fetchJson(`/api/providers/${encodeURIComponent(provider.name)}/models`);
    const models = data.models?.data || [];
    if (models.length === 0) {
      const empty = document.createElement("div");
      empty.className = "model-empty";
      empty.textContent = "未找到可用模型";
      dropdown.appendChild(empty);
      dropdown.hidden = false;
      setMessage(`${provider.name} 未返回模型列表`, "error");
      return;
    }
    models.forEach((model) => {
      const option = document.createElement("button");
      option.type = "button";
      option.textContent = model.id;
      option.addEventListener("click", () => {
        modelInput.value = model.id;
        providers[index].model = model.id;
        dropdown.hidden = true;
        setMessage(`已选择模型 ${model.id}`, "ok");
        refreshSummary();
      });
      dropdown.appendChild(option);
    });
    dropdown.hidden = false;
    setMessage(`已获取 ${models.length} 个模型`, "ok");
  } catch (error) {
    const errorNode = document.createElement("div");
    errorNode.className = "model-empty";
    errorNode.textContent = `获取失败：${error.message}`;
    dropdown.appendChild(errorNode);
    dropdown.hidden = false;
    setMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "获取模型";
  }
}

function formatTime(value) {
  if (!value) return "-";
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}

function renderEmptyRequestRow(text) {
  requestLogBody.innerHTML = "";
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 8;
  cell.className = "empty-cell";
  cell.textContent = text;
  row.appendChild(cell);
  requestLogBody.appendChild(row);
}

function appendRequestCell(row, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text;
  row.appendChild(cell);
}

function renderOverviewRequests(requests) {
  overviewRequestList.innerHTML = "";
  const recentRequests = requests.slice(0, 4);

  if (recentRequests.length === 0) {
    overviewRequestList.innerHTML = '<p class="mini-empty">暂无请求。配置完成后，客户端调用会显示在这里。</p>';
    return;
  }

  recentRequests.forEach((entry) => {
    const item = document.createElement("div");
    item.className = "mini-item";

    const title = document.createElement("strong");
    title.textContent = `${entry.provider || "-"} · ${entry.status ?? "-"}`;

    const meta = document.createElement("span");
    meta.textContent = `${formatTime(entry.time)} · ${entry.latency_ms ?? "-"} ms · ${entry.streamed ? "stream" : "json"}`;

    const dot = document.createElement("i");
    dot.className = Number(entry.status) === 200 ? "status-dot ok" : "status-dot error";

    item.append(dot, title, meta);
    overviewRequestList.appendChild(item);
  });
}

function filteredRequests() {
  const status = requestStatusFilter.value;
  const query = requestSearchInput.value.trim().toLowerCase();

  return latestRequests.filter((entry) => {
    const statusMatched =
      status === "all" ||
      (status === "ok" && Number(entry.status) === 200) ||
      (status === "error" && Number(entry.status) !== 200) ||
      (status === "stream" && Boolean(entry.streamed));

    const searchable = [
      entry.provider,
      entry.status,
      entry.path,
      entry.stream_status,
      entry.error,
    ]
      .filter((value) => value !== undefined && value !== null)
      .join(" ")
      .toLowerCase();

    return statusMatched && (!query || searchable.includes(query));
  });
}

function renderRequests(requests) {
  requestLogBody.innerHTML = "";
  latestRequests = requests;
  renderOverviewRequests(latestRequests);
  const visibleRequests = filteredRequests();

  if (!requests.length) {
    renderEmptyRequestRow("暂无请求记录。配置完成后，把客户端 Base URL 指向 http://127.0.0.1:8000/v1，这里就会出现调用轨迹。");
    return;
  }

  if (!visibleRequests.length) {
    renderEmptyRequestRow("没有符合筛选条件的请求。可以切回“全部”或清空搜索关键词。");
    return;
  }

  visibleRequests.forEach((entry) => {
    const row = document.createElement("tr");
    const statusClass = Number(entry.status) === 200 ? "ok-text" : "error-text";
    appendRequestCell(row, formatTime(entry.time));
    appendRequestCell(row, entry.provider || "-");
    appendRequestCell(row, String(entry.status ?? "-"), statusClass);
    appendRequestCell(row, `${entry.latency_ms ?? "-"} ms`);
    appendRequestCell(row, String(entry.fallback_count ?? 0));
    appendRequestCell(row, entry.streamed ? "stream" : "json");
    appendRequestCell(row, entry.stream_status || "-");
    appendRequestCell(row, entry.error || "", "error-detail");
    requestLogBody.appendChild(row);
  });
}

async function loadRequests() {
  try {
    const data = await fetchJson("/api/requests");
    renderRequests(data.requests || []);
  } catch (error) {
    renderEmptyRequestRow(`读取失败：${error.message}`);
  }
}

function addProvider() {
  providers.push({
    name: `provider-${providers.length + 1}`,
    protocol: "openai",
    base_url: "https://example.com/v1",
    model: "",
    priority: providers.length,
    api_keys_text: "",
    api_key_env: "",
    has_api_key: false,
    key_count: 0,
    enabled: true,
    use_curl: false,
    model_aliases: {},
    calls: 0,
    last_remaining: null,
  });
  renderProviders();
}

async function copyText(targetId) {
  const target = document.querySelector(`#${targetId}`);
  if (!target) return;
  const text = target.textContent.trim();
  try {
    await navigator.clipboard.writeText(text);
    setMessage("已复制到剪贴板", "ok");
  } catch (error) {
    setMessage("复制失败，请手动选择文本", "error");
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy-target]");
  if (!button) return;
  copyText(button.dataset.copyTarget);
});

document.addEventListener("click", (event) => {
  const viewButton = event.target.closest("[data-view-target]");
  if (viewButton) {
    setActiveView(viewButton.dataset.viewTarget);
    return;
  }

  const jumpButton = event.target.closest("[data-view-jump]");
  if (jumpButton) {
    setActiveView(jumpButton.dataset.viewJump);
  }
});

document.querySelector("#addProviderBtn").addEventListener("click", addProvider);
document.querySelector("#saveBtn").addEventListener("click", () => {
  saveConfig().catch(() => {});
});
saveAndTestBtn.addEventListener("click", saveAndTestAll);
document.querySelector("#exportConfigBtn").addEventListener("click", exportConfig);
document.querySelector("#importConfigBtn").addEventListener("click", () => importConfigFile.click());
importConfigFile.addEventListener("change", () => importConfig(importConfigFile.files?.[0]));
document.querySelector("#saveProxyTokenBtn").addEventListener("click", () => {
  localStorage.setItem("gpt_proxy_access_token", localProxyTokenInput.value.trim());
  setMessage("本地代理访问密钥已保存到浏览器", "ok");
  loadConfig().catch((error) => setMessage(error.message, "error"));
});
document.querySelector("#clearProxyTokenBtn").addEventListener("click", () => {
  localStorage.removeItem("gpt_proxy_access_token");
  localProxyTokenInput.value = "";
  setMessage("本地代理访问密钥已清除", "ok");
});
document.querySelector("#refreshRequestsBtn").addEventListener("click", loadRequests);
requestStatusFilter.addEventListener("change", () => renderRequests(latestRequests));
requestSearchInput.addEventListener("input", () => renderRequests(latestRequests));
document.querySelector("#refreshBtn").addEventListener("click", () => {
  loadConfig().catch((error) => setMessage(error.message, "error"));
});
defaultModelInput.addEventListener("input", refreshSummary);
setActiveView(location.hash.replace("#", "") || "overview", false);

loadConfig().catch((error) => {
  healthBadge.textContent = "服务异常";
  healthBadge.className = "badge muted";
  setMessage(error.message, "error");
});
