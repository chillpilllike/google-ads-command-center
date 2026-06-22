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

  function findTextInput({ preferMultiline = false } = {}) {
    const candidates = deepElements().filter((node) => {
      if (!visible(node)) return false;
      const tag = node.tagName.toLowerCase();
      const type = String(node.getAttribute("type") || "").toLowerCase();
      return tag === "textarea" || (tag === "input" && !["hidden", "checkbox", "radio", "file"].includes(type)) || node.isContentEditable;
    });
    if (preferMultiline) {
      return candidates.find((node) => node.tagName.toLowerCase() === "textarea") || candidates.find((node) => node.isContentEditable) || candidates[0];
    }
    return candidates[0];
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

  function taskValues(task) {
    const payload = task.payload || {};
    const values = Array.isArray(payload.batch_values) ? payload.batch_values : [];
    const cleaned = values.map((value) => String(value || "").trim()).filter(Boolean);
    if (cleaned.length) return [...new Set(cleaned)];
    const value = mainValue(payload);
    return value ? [value] : [];
  }

  function batchTaskIds(task) {
    const payload = task.payload || {};
    const ids = Array.isArray(payload.batch_task_ids) ? payload.batch_task_ids : [task.id];
    return [...new Set(ids.map((value) => Number(value)).filter((value) => Number.isFinite(value) && value > 0))];
  }

  function keywordForPaste(value) {
    const text = String(value || "").trim().replace(/^\[|\]$/g, "");
    return text ? `[${text}]` : "";
  }

  function valuesForPaste(task) {
    const values = taskValues(task);
    if (task.action_type === "add_keyword" || task.action_type === "add_negative_keyword") {
      return values.map(keywordForPaste).filter(Boolean);
    }
    return values;
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
        <div class="gacc-muted">Values copied for paste</div>
        <div class="gacc-value">${escapeHtml(`${taskValues(task).length} item(s)\n${taskValues(task).slice(0, 12).join("\n")}${taskValues(task).length > 12 ? "\n..." : ""}`)}</div>
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

  function possibleValidationText() {
    return deepElements()
      .filter(visible)
      .map(textOf)
      .find((text) => /\b(error|invalid|required|cannot|failed|disapproved|not eligible)\b/i.test(text));
  }

  async function selectExactUrlMode() {
    const exact = findClickableByText(["use exact urls", "exact urls", "exact url"]);
    if (exact) {
      exact.click();
      await sleep(400);
      return true;
    }
    const radios = deepElements()
      .filter(visible)
      .filter((node) => String(node.getAttribute("role") || "").toLowerCase() === "radio" || String(node.getAttribute("type") || "").toLowerCase() === "radio");
    const exactRadio = radios.find((node) => /exact url/i.test(textOf(node.closest("label") || node.parentElement || node)));
    if (exactRadio) {
      exactRadio.click();
      await sleep(400);
      return true;
    }
    return false;
  }

  async function guardedAddFlow(task) {
    const payload = task.payload || {};
    const values = valuesForPaste(task);
    if (!values.length) {
      return { status: "needs_manual_attention", reason: "No values found in task payload." };
    }
    const addButton = findClickableByText(["add", "new", "plus", "create"]);
    if (!addButton) {
      return { status: "needs_manual_attention", reason: "Could not find a visible Add/Create button." };
    }
    addButton.click();
    await sleep(1000);
    if (task.action_type === "add_url_inclusion" || task.action_type === "add_url_exclusion") {
      await selectExactUrlMode();
    }
    const input = findTextInput({ preferMultiline: values.length > 1 || task.action_type.includes("url") || task.action_type.includes("keyword") });
    if (!input) {
      return { status: "needs_manual_attention", reason: "Add dialog opened, but no editable input was found." };
    }
    const pasteValue = values.join("\n");
    setInputValue(input, pasteValue);
    await sleep(350);
    const intermediateAdd = values.length > 1 ? findClickableByText(["add urls", "add keywords", "add"]) : null;
    if (intermediateAdd && intermediateAdd !== addButton) {
      intermediateAdd.click();
      await sleep(900);
    }
    const save = findClickableByText(["save", "apply", "done"]);
    if (!save) {
      return { status: "needs_manual_attention", reason: "Value filled, but no Save/Apply button was found." };
    }
    save.click();
    await sleep(Math.min(6000, 1600 + values.length * 35));
    const errorText = possibleValidationText();
    if (errorText) {
      return { status: "needs_manual_attention", reason: `Google Ads showed a possible validation message: ${errorText.slice(0, 220)}` };
    }
    return { status: "done", reason: `Clicked add, filled ${values.length} value(s), and clicked save/apply.` };
  }

  async function executeTask(task) {
    const payload = task.payload || {};
    renderPanel(task, "Preparing task...");
    await copyToClipboard(valuesForPaste(task).join("\n") || mainValue(payload));
    const safeAddActions = new Set(["add_keyword", "add_negative_keyword", "add_url_inclusion", "add_url_exclusion", "add_pmax_search_theme"]);
    if (!safeAddActions.has(task.action_type)) {
      await copyToClipboard(JSON.stringify(payload.raw_editor_row || payload, null, 2));
      renderPanel(task, "Complex campaign/ad/entity creation is queued and copied as structured JSON. Use the Google Ads Editor import bundle for this step until the wizard template is trained.");
      return {
        status: "needs_manual_attention",
        reason: "Complex wizard step requires Google Ads Editor import or supervised browser flow.",
        copied_value: mainValue(payload),
        current_url: location.href,
        batch_task_ids: batchTaskIds(task),
      };
    }
    const result = await guardedAddFlow(task);
    renderPanel(task, result.reason);
    return {
      status: result.status,
      reason: result.reason,
      copied_value: valuesForPaste(task).join("\n") || mainValue(payload),
      value_count: taskValues(task).length,
      batch_task_ids: batchTaskIds(task),
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
