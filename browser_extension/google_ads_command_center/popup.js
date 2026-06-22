const fields = ["appBaseUrl", "token", "accountId", "batchSize"];
const statusBox = document.getElementById("status");

function setStatus(message) {
  statusBox.textContent = message;
}

async function loadConfig() {
  const saved = await chrome.storage.local.get([...fields, "running", "lastStatus"]);
  for (const field of fields) {
    document.getElementById(field).value = saved[field] || (field === "appBaseUrl" ? "https://googleads.gofinch.com" : "");
  }
  setStatus(saved.lastStatus || (saved.running ? "Running." : "Stopped."));
}

async function saveConfig() {
  const payload = {};
  for (const field of fields) {
    payload[field] = document.getElementById(field).value.trim();
  }
  await chrome.storage.local.set(payload);
  setStatus("Saved.");
}

async function sendCommand(command) {
  await saveConfig();
  const response = await chrome.runtime.sendMessage({ type: command });
  setStatus(response?.message || JSON.stringify(response || {}));
}

document.getElementById("save").addEventListener("click", saveConfig);
document.getElementById("start").addEventListener("click", () => sendCommand("START"));
document.getElementById("stop").addEventListener("click", () => sendCommand("STOP"));
document.getElementById("runOnce").addEventListener("click", () => sendCommand("RUN_ONCE"));
document.getElementById("statusBtn").addEventListener("click", () => sendCommand("STATUS"));

loadConfig();
