const DEFAULT_POLL_MS = 3500;
const WORKER_ID_KEY = "workerId";
let pollTimer = null;
let runningStep = false;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function storageGet(keys) {
  return chrome.storage.local.get(keys);
}

function storageSet(values) {
  return chrome.storage.local.set(values);
}

async function workerId() {
  const saved = await storageGet(WORKER_ID_KEY);
  if (saved.workerId) return saved.workerId;
  const id = `chrome-worker-${crypto.randomUUID()}`;
  await storageSet({ workerId: id });
  return id;
}

function normalizeBaseUrl(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

async function config() {
  const saved = await storageGet(["appBaseUrl", "token", "accountId", "pollMs", "running"]);
  return {
    appBaseUrl: normalizeBaseUrl(saved.appBaseUrl),
    token: String(saved.token || "").trim(),
    accountId: String(saved.accountId || "").trim(),
    pollMs: Number(saved.pollMs || DEFAULT_POLL_MS),
    running: Boolean(saved.running),
  };
}

async function apiFetch(path, options = {}) {
  const cfg = await config();
  if (!cfg.appBaseUrl || !cfg.token) {
    throw new Error("Missing app URL or token.");
  }
  const url = new URL(`${cfg.appBaseUrl}${path}`);
  if (cfg.accountId && !url.searchParams.has("account_id")) {
    url.searchParams.set("account_id", cfg.accountId);
  }
  const response = await fetch(url.toString(), {
    ...options,
    headers: {
      "Authorization": `Bearer ${cfg.token}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`App API ${response.status}: ${text.slice(0, 240)}`);
  }
  return response.json();
}

async function updateStatus(message) {
  await storageSet({ lastStatus: `${new Date().toLocaleString()}\n${message}` });
}

async function findOrCreateAdsTab(targetUrl) {
  const tabs = await chrome.tabs.query({ url: "https://ads.google.com/*" });
  let tab = tabs.find((item) => item.active) || tabs[0];
  if (!tab) {
    tab = await chrome.tabs.create({ url: targetUrl || "https://ads.google.com/aw/overview", active: true });
  } else if (targetUrl && !String(tab.url || "").startsWith(targetUrl.split("?")[0])) {
    tab = await chrome.tabs.update(tab.id, { url: targetUrl, active: true });
  } else {
    await chrome.tabs.update(tab.id, { active: true });
  }
  await waitForTab(tab.id);
  return chrome.tabs.get(tab.id);
}

function waitForTab(tabId) {
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }, 25000);
    function listener(updatedTabId, changeInfo) {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function ensureContentScript(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "PING" });
    return;
  } catch (_error) {
    await chrome.scripting.insertCSS({ target: { tabId }, files: ["content.css"] }).catch(() => {});
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  }
}

async function sendResult(task, status, result) {
  await apiFetch(`/api/browser-automation/tasks/${task.id}/result`, {
    method: "POST",
    body: JSON.stringify({
      worker_id: await workerId(),
      status,
      result,
    }),
  });
}

async function runOneStep() {
  if (runningStep) return { ok: false, message: "A step is already running." };
  runningStep = true;
  try {
    const id = await workerId();
    const next = await apiFetch(`/api/browser-automation/next?worker_id=${encodeURIComponent(id)}`);
    const task = next.task;
    if (!task) {
      await updateStatus("No queued browser automation tasks.");
      return { ok: true, message: "No queued tasks." };
    }
    const payload = task.payload || {};
    const tab = await findOrCreateAdsTab(payload.target_url || "https://ads.google.com/aw/overview");
    await ensureContentScript(tab.id);
    await sleep(1500);
    const response = await chrome.tabs.sendMessage(tab.id, { type: "EXECUTE_BROWSER_AUTOMATION_TASK", task });
    const status = response?.status || "needs_manual_attention";
    await sendResult(task, status, response || {});
    await updateStatus(`Task #${task.id}: ${status}\n${task.action_type}\n${payload.campaign || ""}`);
    return { ok: true, message: `Task #${task.id}: ${status}` };
  } catch (error) {
    await updateStatus(`Worker error: ${error.message}`);
    return { ok: false, message: error.message };
  } finally {
    runningStep = false;
  }
}

async function pollLoop() {
  const cfg = await config();
  if (!cfg.running) return;
  await runOneStep();
  const fresh = await config();
  if (fresh.running) {
    pollTimer = setTimeout(pollLoop, Math.max(1000, fresh.pollMs || DEFAULT_POLL_MS));
  }
}

async function start() {
  await storageSet({ running: true });
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(pollLoop, 100);
  return { ok: true, message: "Started browser worker." };
}

async function stop() {
  await storageSet({ running: false });
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
  await updateStatus("Stopped.");
  return { ok: true, message: "Stopped browser worker." };
}

async function status() {
  const cfg = await config();
  const remote = cfg.appBaseUrl && cfg.token ? await apiFetch("/api/browser-automation/status") : null;
  return {
    ok: true,
    message: `${cfg.running ? "Running" : "Stopped"}\n${remote ? JSON.stringify(remote.counts, null, 2) : "No app connection configured."}`,
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.type === "START") return start();
    if (message.type === "STOP") return stop();
    if (message.type === "RUN_ONCE") return runOneStep();
    if (message.type === "STATUS") return status();
    return { ok: false, message: "Unknown command." };
  })().then(sendResponse).catch((error) => sendResponse({ ok: false, message: error.message }));
  return true;
});

chrome.runtime.onInstalled.addListener(() => {
  updateStatus("Installed. Paste app URL and token, then start.");
});
