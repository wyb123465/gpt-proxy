const providerList = document.querySelector("#providerList");
const providerTemplate = document.querySelector("#providerTemplate");
const defaultModelInput = document.querySelector("#defaultModel");
const saveMessage = document.querySelector("#saveMessage");
const healthBadge = document.querySelector("#healthBadge");
const usableCount = document.querySelector("#usableCount");
const totalCalls = document.querySelector("#totalCalls");
const modelSummary = document.querySelector("#modelSummary");
const requestLogBody = document.querySelector("#requestLogBody");
const proxyTokenState = document.querySelector("#proxyTokenState");
const encryptionState = document.querySelector("#encryptionState");
const rateLimitState = document.querySelector("#rateLimitState");
const bodyLimitState = document.querySelector("#bodyLimitState");
const cooldownState = document.querySelector("#cooldownState");
const importConfigFile = document.querySelector("#importConfigFile");
const localProxyTokenInput = document.querySelector("#localProxyToken");

let providers = [];
localProxyTokenInput.value = localStorage.getItem("gpt_proxy_access_token") || "";

function setMessage(text, type = "") {
  saveMessage.textContent = text;
  saveMessage.className = `save-message ${type}`.trim();
}

function providerMeta(provider) {
  const enabled = provider.enabled === false ? "已停用" : "已启用";
  const keyState = provider.has_api_key || provider.api_key || provider.api_keys_text ? `密钥 ${provider.key_count || 1} 个` : "未设置密钥";
  const calls = Number(provider.calls || 0).toLocaleString("zh-CN");
  const remaining = provider.last_remaining ?? "未知";
  return `${enabled} · ${keyState} · 调用 ${calls} 次 · 剩余额度 ${remaining}`;
}

function renderSecurity(security = {}) {
  proxyTokenState.textContent = security.proxy_access_token_enabled ? "已启用" : "未启用";
  encryptionState.textContent = security.config_encryption_enabled ? "已启用" : "未启用";
  rateLimitState.textContent = security.rate_limit_per_minute > 0 ? `${security.rate_limit_per_minute}/分钟` : "未启用";
  bodyLimitState.textContent = security.max_request_bytes ? `${Math.round(security.max_request_bytes / 1024)} KB` : "未限制";
  cooldownState.textContent = security.key_cooldown_seconds ? `${security.key_cooldown_seconds} 秒` : "未启用";
}

function renderProviders() {
  providerList.innerHTML = "";
  providers.forEach((provider, index) => {
    if (provider.enabled === undefined) provider.enabled = true;

    const node = providerTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.index = index;
    node.querySelector(".provider-title").textContent = provider.name || "新 API";
    node.querySelector(".provider-meta").textContent = providerMeta(provider);

    for (const input of node.querySelectorAll("[data-field]")) {
      const field = input.dataset.field;
      if (field === "api_keys_text") {
        input.value = provider.api_keys_text || "";
      } else if (input.type === "checkbox") {
        input.checked = Boolean(provider[field]);
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
  refreshSummary();
}

function refreshSummary() {
  usableCount.textContent = providers.filter((provider) => provider.enabled !== false && (provider.has_api_key || provider.api_key || provider.api_keys_text)).length;
  totalCalls.textContent = providers
    .reduce((total, provider) => total + Number(provider.calls || 0), 0)
    .toLocaleString("zh-CN");
  modelSummary.textContent = defaultModelInput.value || "未设置";
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
  const [health, config] = await Promise.all([
    fetchJson("/health").catch(() => null),
    fetchJson("/api/config"),
  ]);

  healthBadge.textContent = health ? "服务正常" : "服务异常";
  healthBadge.className = health ? "badge" : "badge muted";
  defaultModelInput.value = config.default_model || "gpt-4o";
  providers = config.providers || [];
  renderSecurity(config.security);
  renderProviders();
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
  } catch (error) {
    setMessage(error.message, "error");
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
      dropdown.innerHTML = '<div class="model-empty">未找到可用模型</div>';
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
    dropdown.innerHTML = `<div class="model-empty">获取失败：${error.message}</div>`;
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

function renderRequests(requests) {
  requestLogBody.innerHTML = "";
  if (!requests.length) {
    requestLogBody.innerHTML = '<tr><td colspan="8" class="empty-cell">暂无请求记录</td></tr>';
    return;
  }
  requests.forEach((entry) => {
    const row = document.createElement("tr");
    const statusClass = Number(entry.status) === 200 ? "ok-text" : "error-text";
    row.innerHTML = `
      <td>${formatTime(entry.time)}</td>
      <td>${entry.provider || "-"}</td>
      <td class="${statusClass}">${entry.status}</td>
      <td>${entry.latency_ms ?? "-"} ms</td>
      <td>${entry.fallback_count ?? 0}</td>
      <td>${entry.streamed ? "stream" : "json"}</td>
      <td>${entry.stream_status || "-"}</td>
      <td class="error-detail">${entry.error || ""}</td>
    `;
    requestLogBody.appendChild(row);
  });
}

async function loadRequests() {
  try {
    const data = await fetchJson("/api/requests");
    renderRequests(data.requests || []);
  } catch (error) {
    requestLogBody.innerHTML = `<tr><td colspan="8" class="empty-cell">读取失败：${error.message}</td></tr>`;
  }
}

function addProvider() {
  providers.push({
    name: `provider-${providers.length + 1}`,
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

document.querySelector("#addProviderBtn").addEventListener("click", addProvider);
document.querySelector("#saveBtn").addEventListener("click", saveConfig);
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
document.querySelector("#refreshBtn").addEventListener("click", () => {
  loadConfig().catch((error) => setMessage(error.message, "error"));
});
defaultModelInput.addEventListener("input", refreshSummary);

loadConfig().catch((error) => {
  healthBadge.textContent = "服务异常";
  healthBadge.className = "badge muted";
  setMessage(error.message, "error");
});
