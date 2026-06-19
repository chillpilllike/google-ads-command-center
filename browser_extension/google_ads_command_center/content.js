(() => {
  if (window.__gaccWorkerLoaded) return;
  window.__gaccWorkerLoaded = true;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function textOf(node) {
    return String(node?.innerText || node?.textContent || node?.getAttribute?.("aria-label") || "").replace(/\s+/g, " ").trim();
  }

  function visible(node) {
    if (!node || !(node instanceof Element)) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
  }

  function deepElements(root = document) {
    const output = [];
    const visit = (node) => {
      if (!node) return;
      if (node instanceof Element) {
        output.push(node);
        if (node.shadowRoot) visit(node.shadowRoot);
      }
      for (const child of node.children || []) visit(child);
    };
    visit(root);
    return output;
  }

  function findClickableByText(patterns) {
    const lowered = patterns.map((value) => String(value).toLowerCase());
    return deepElements()
      .filter(visible)
      .filter((node) => {
        const tag = node.tagName.toLowerCase();
        const role = String(node.getAttribute("role") || "").toLowerCase();
        return tag === "button" || tag === "material-button" || role === "button" || node.hasAttribute("aria-label");
      })
      .find((node) => {
        const text = textOf(node).toLowerCase();
        return lowered.some((pattern) => text.includes(pattern));
      });
  }

  function findTextInput() {
    return deepElements().find((node) => {
      if (!visible(node)) return false;
      const tag = node.tagName.toLowerCase();
      const type = String(node.getAttribute("type") || "").toLowerCase();
      return tag === "textarea" || (tag === "input" && !["hidden", "checkbox", "radio", "file"].includes(type)) || node.isContentEditable;
    });
  }

  function setInputValue(node, value) {
    if (!node) return false;
    node.focus();
    if (node.isContentEditable) {
      node.textContent = value;
      node.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
      return true;
    }
    const descriptor = Object.getOwnPropertyDescriptor(node.constructor.prototype, "value");
    if (descriptor?.set) descriptor.set.call(node, value);
    else node.value = value;
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  async function copyToClipboard(value) {
    try {
      await navigator.clipboard.writeText(String(value || ""));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function mainValue(payload) {
    return payload.keyword || payload.search_theme || payload.url || payload.ad_type || payload.campaign || "";
  }

  function renderPanel(task, note = "") {
    let panel = document.getElementById("gacc-worker-panel");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "gacc-worker-panel";
      document.documentElement.appendChild(panel);
    }
    const payload = task.payload || {};
    panel.innerHTML = `
      <header>
        <span>Ads Worker Step #${task.id}</span>
        <button type="button" id="gacc-close">Hide</button>
      </header>
      <main>
        <div><strong>${task.action_type}</strong> <span class="gacc-muted">${task.entity_type || ""}</span></div>
        <div class="gacc-muted">Campaign</div>
        <div class="gacc-value">${escapeHtml(payload.campaign || "")}</div>
        <div class="gacc-muted">Ad group / Asset group</div>
        <div class="gacc-value">${escapeHtml(payload.ad_group || payload.asset_group || "")}</div>
        <div class="gacc-muted">Value copied for paste</div>
        <div class="gacc-value">${escapeHtml(mainValue(payload))}</div>
        ${note ? `<div class="gacc-value">${escapeHtml(note)}</div>` : ""}
      </main>
    `;
    panel.querySelector("#gacc-close")?.addEventListener("click", () => panel.remove());
  }

  function escapeHtml(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  async function guardedAddFlow(task) {
    const payload = task.payload || {};
    const value = mainValue(payload);
    if (!value) {
      return { status: "needs_manual_attention", reason: "No value found in task payload." };
    }
    const addButton = findClickableByText(["add", "new", "plus", "create"]);
    if (!addButton) {
      return { status: "needs_manual_attention", reason: "Could not find a visible Add/Create button." };
    }
    addButton.click();
    await sleep(1000);
    const input = findTextInput();
    if (!input) {
      return { status: "needs_manual_attention", reason: "Add dialog opened, but no editable input was found." };
    }
    setInputValue(input, value);
    await sleep(350);
    const save = findClickableByText(["save", "apply", "done"]);
    if (!save) {
      return { status: "needs_manual_attention", reason: "Value filled, but no Save/Apply button was found." };
    }
    save.click();
    await sleep(1800);
    const errorText = deepElements()
      .filter(visible)
      .map(textOf)
      .find((text) => /\b(error|invalid|required|cannot|failed)\b/i.test(text));
    if (errorText) {
      return { status: "needs_manual_attention", reason: `Google Ads showed a possible validation message: ${errorText.slice(0, 220)}` };
    }
    return { status: "done", reason: "Clicked add, filled the value, and clicked save/apply." };
  }

  async function executeTask(task) {
    const payload = task.payload || {};
    renderPanel(task, "Preparing task...");
    await copyToClipboard(mainValue(payload));
    const safeAddActions = new Set(["add_keyword", "add_negative_keyword", "add_url_inclusion", "add_url_exclusion", "add_pmax_search_theme"]);
    if (!safeAddActions.has(task.action_type)) {
      renderPanel(task, "Complex campaign/ad/entity creation is prepared in the panel. Use the copied payload or Google Ads Editor import for this step.");
      return {
        status: "needs_manual_attention",
        reason: "Complex wizard step requires supervised browser flow.",
        copied_value: mainValue(payload),
        current_url: location.href,
      };
    }
    const result = await guardedAddFlow(task);
    renderPanel(task, result.reason);
    return {
      status: result.status,
      reason: result.reason,
      copied_value: mainValue(payload),
      current_url: location.href,
    };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    (async () => {
      if (message.type === "PING") return { ok: true };
      if (message.type === "EXECUTE_BROWSER_AUTOMATION_TASK") {
        return executeTask(message.task);
      }
      return { ok: false, status: "failed", reason: "Unknown content message." };
    })().then(sendResponse).catch((error) => sendResponse({ ok: false, status: "failed", reason: error.message }));
    return true;
  });
})();
