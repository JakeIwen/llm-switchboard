// Modal dialogs. Enter confirms (unless a different button has focus) and
// Escape cancels, in place of the browser's confirm()/prompt(). The confirm
// button stays disabled until every required field has a value.

// fields: [{ name, label, kind: "text" | "select", value, placeholder, list,
//            required, options: [{ value, label, disabled }] }]
function open({ title, body, confirm, danger, fields = [] }) {
  const dlg = document.createElement("dialog");
  dlg.className = "dlg";
  dlg.innerHTML = `<form method="dialog">
    <h2></h2><div class="dlg-body"></div><div class="dlg-fields"></div>
    <div class="dlg-buttons">
      <button type="button" class="btn" data-cancel>Cancel</button>
      <button type="submit" class="btn primary ${danger ? "danger-fill" : ""}" value="ok"></button>
    </div></form>`;
  dlg.querySelector("h2").textContent = title;
  const bodyEl = dlg.querySelector(".dlg-body");
  if (body instanceof Node) bodyEl.append(body); else bodyEl.textContent = body || "";
  if (!body) bodyEl.remove();
  const ok = dlg.querySelector("[value=ok]");
  ok.textContent = confirm;
  const inputs = fields.map((f) => addField(dlg.querySelector(".dlg-fields"), f));
  const valid = () => fields.every((f, i) => !f.required || inputs[i].value.trim());
  const sync = () => { ok.disabled = !valid(); };
  for (const el of inputs) el.addEventListener("input", sync);
  sync();
  document.body.append(dlg);

  return new Promise((resolve) => {
    let result = false;
    dlg.querySelector("[data-cancel]").addEventListener("click", () => dlg.close());
    dlg.querySelector("form").addEventListener("submit", (ev) => {
      if (!valid()) { ev.preventDefault(); return; }
      result = true;
    });
    dlg.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" || ev.isComposing) return;
      // Enter on a focused Cancel button cancels; anywhere else it confirms.
      if (ev.target instanceof HTMLButtonElement && ev.target !== ok) return;
      ev.preventDefault();
      if (!valid()) return;
      result = true;
      dlg.close();
    });
    dlg.addEventListener("close", () => {
      dlg.remove();
      if (!fields.length) return resolve(result);
      resolve(result ? Object.fromEntries(fields.map((f, i) => [f.name, inputs[i].value])) : null);
    });
    dlg.showModal();
    const first = inputs.find((el, i) => fields[i].autofocus) || inputs[0];
    (first || ok).focus();
    if (first instanceof HTMLInputElement) first.select();
  });
}

function addField(container, f) {
  const wrap = document.createElement("label");
  wrap.className = "dlg-field";
  if (f.label) wrap.append(Object.assign(document.createElement("span"), { textContent: f.label }));
  let el;
  if (f.kind === "select") {
    el = document.createElement("select");
    for (const o of f.options) {
      el.append(Object.assign(document.createElement("option"), { value: o.value, textContent: o.label, disabled: Boolean(o.disabled) }));
    }
    el.value = f.value ?? f.options.find((o) => !o.disabled)?.value ?? "";
  } else {
    el = Object.assign(document.createElement("input"), {
      type: "text", value: f.value || "", placeholder: f.placeholder || "", autocomplete: "off", spellcheck: false,
    });
    if (f.list) el.setAttribute("list", f.list);
    if (f.maxlength) el.maxLength = f.maxlength;
  }
  el.className = "dlg-input";
  el.name = f.name;
  wrap.append(el);
  container.append(wrap);
  return el;
}

export const confirmDialog = ({ title, body, confirm = "OK", danger = false }) =>
  open({ title, body, confirm, danger });

// Resolves to the entered text, or null if cancelled.
export const promptDialog = ({ title, body, confirm = "OK", value = "", placeholder = "", list = "" }) =>
  open({ title, body, confirm, fields: [{ name: "value", value, placeholder, list }] }).then((r) => r && r.value);

// Resolves to { [field name]: value }, or null if cancelled.
export const formDialog = ({ title, body, confirm = "OK", fields }) => open({ title, body, confirm, fields });
