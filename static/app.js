const providerList = document.querySelector("#providerList");
const providerTemplate = document.querySelector("#providerTemplate");
const defaultModelInput = document.querySelector("#defaultModel");
const saveMessage = document.querySelector("#saveMessage");
const healthBadge = document.querySelector("#healthBadge");
const usableCount = document.querySelector("#usableCount");
const totalCalls = document.querySelector("#totalCalls");
const modelSummary = document.querySelector("#modelSummary");

let providers = [];

function setMessage(text, type = "") {
  saveMessage.textContent = text;
  saveMessage.className = `save-message ${type}`.trim();
}

function providerMeta(provider) {
  const keyState = provider.has_api_key ? "已设置密钥" : "未设置密钥";
  const calls = Number(provider.calls || 0).toLocaleString("zh-CN");
  const remaining = provider.last_remaining ?? "未知";
  return `${keyState} · 调用 ${calls} 次 · 剩余额度 ${remaining}`;
}

function renderProviders() {
  providerList.innerHTML = "";
  providers.forEach((provider, index) => {
    const node = providerTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.index = index;
    node.querySelector(".provider-title").textContent = provider.name || "新 API";
    node.querySelector(".provider-meta").textContent = providerMeta(provider);

    for (const input of node.querySelectorAll("[data-field]")) {
        const field = input.dataset.field;
        if (input.type === "checkbox") {
          input.checked = Boolean(provider[field]);
        } else {
          input.value = provider[field] ?? "";
        }
        input.addEventListener("input", () => {
          if (input.type === "checkbox") {
            providers[index][field] = input.checked;
          } else if (field === "priority") {
            providers[index][field] = Number(input.value || 0);
          } else {
            providers[index][field] = input.value;
          }
          node.querySelector(".provider-title").textContent = providers[index].name || "新 API";
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
      const key = `alias-${Object.keys(providers[index].model_aliases).length + 1}`;
      providers[index].model_aliases[key] = "";
      renderAliases(aliasList, index);
    });

    providerList.appendChild(node);
  });
  refreshSummary();
}

function refreshSummary() {
  usableCount.textContent = providers.filter((provider) => provider.has_api_key || provider.api_key).length;
  totalCalls.textContent = providers
    .reduce((total, provider) => total + Number(provider.calls || 0), 0)
    .toLocaleString("zh-CN");
  modelSummary.textContent = defaultModelInput.value || "未设置";
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
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
  defaultModelInput.value = config.default_model || "gpt-3.5-turbo";
  providers = config.providers || [];
  renderProviders();
  setMessage("配置已载入", "ok");
}

function collectPayload() {
    return {
      default_model: defaultModelInput.value.trim() || "gpt-3.5-turbo",
      providers: providers.map((provider) => ({
        name: String(provider.name || "").trim(),
        base_url: String(provider.base_url || "").trim(),
        model: String(provider.model || "").trim(),
        priority: Number(provider.priority || 0),
        api_key: String(provider.api_key || "").trim(),
        api_key_env: String(provider.api_key_env || "").trim(),
        use_curl: Boolean(provider.use_curl),
        model_aliases: provider.model_aliases && typeof provider.model_aliases === "object"
          ? Object.fromEntries(
              Object.entries(provider.model_aliases)
                .filter(([k, v]) => k.trim() && v.trim())
                .map(([k, v]) => [k.trim(), v.trim()])
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
    renderProviders();
    setMessage("已保存，下一次请求会使用新配置", "ok");
  } catch (error) {
    setMessage(error.message, "error");
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
        setMessage(`${provider.name} 还没有填写密钥`, "error");
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
        const val = providers[index].model_aliases[oldKey];
        delete providers[index].model_aliases[oldKey];
        providers[index].model_aliases[newKey] = val;
      }
    });
    toInput.addEventListener("input", () => {
      providers[index].model_aliases[fromInput.value.trim()] = toInput.value.trim();
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

  const btn = node.querySelector(".fetch-models");
  const dropdown = node.querySelector(".model-dropdown");
  const modelInput = node.querySelector('[data-field="model"]');
  btn.disabled = true;
  btn.textContent = "获取中";
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
    models.forEach((m) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = m.id;
      btn.addEventListener("click", () => {
        modelInput.value = m.id;
        providers[index].model = m.id;
        dropdown.hidden = true;
        setMessage(`已选择模型 ${m.id}`, "ok");
      });
      dropdown.appendChild(btn);
    });
    dropdown.hidden = false;
    setMessage(`已获取 ${models.length} 个模型`, "ok");
  } catch (error) {
    dropdown.innerHTML = `<div class="model-empty">获取失败：${error.message}</div>`;
    dropdown.hidden = false;
    setMessage(error.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "获取模型";
  }
}

function addProvider() {
    providers.push({
      name: `provider-${providers.length + 1}`,
      base_url: "https://example.com/v1",
      model: defaultModelInput.value || "gpt-3.5-turbo",
      priority: providers.length,
      api_key: "",
      api_key_env: "",
      has_api_key: false,
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
document.querySelector("#refreshBtn").addEventListener("click", () => {
  loadConfig().catch((error) => setMessage(error.message, "error"));
});
defaultModelInput.addEventListener("input", refreshSummary);

loadConfig().catch((error) => {
  healthBadge.textContent = "服务异常";
  healthBadge.className = "badge muted";
  setMessage(error.message, "error");
});

