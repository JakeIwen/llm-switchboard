// Modal dialogs. Enter confirms (unless a different button has focus) and
// Escape cancels, in place of the browser's confirm()/prompt().

function open({ title, body, confirm, danger, input }) {
  const dlg = document.createElement("dialog");
  dlg.className = "dlg";
  dlg.innerHTML = `<form method="dialog">
    <h2></h2><div class="dlg-body"></div>
    ${input ? `<input type="text" class="dlg-input" autocomplete="off" spellcheck="false">` : ""}
    <div class="dlg-buttons">
      <button type="button" class="btn" data-cancel>Cancel</button>
      <button type="submit" class="btn primary ${danger ? "danger-fill" : ""}" value="ok"></button>
    </div></form>`;
  dlg.querySelector("h2").textContent = title;
  const bodyEl = dlg.querySelector(".dlg-body");
  if (body instanceof Node) bodyEl.append(body); else bodyEl.textContent = body || "";
  const ok = dlg.querySelector("[value=ok]");
  ok.textContent = confirm;
  const field = dlg.querySelector(".dlg-input");
  if (field) {
    field.value = input.value || "";
    field.placeholder = input.placeholder || "";
    if (input.list) field.setAttribute("list", input.list);
  }
  document.body.append(dlg);

  return new Promise((resolve) => {
    let result = false;
    dlg.querySelector("[data-cancel]").addEventListener("click", () => dlg.close());
    dlg.querySelector("form").addEventListener("submit", () => { result = true; });
    dlg.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" || ev.isComposing) return;
      // Enter on a focused Cancel button cancels; anywhere else it confirms.
      if (ev.target instanceof HTMLButtonElement && ev.target !== ok) return;
      ev.preventDefault();
      result = true;
      dlg.close();
    });
    dlg.addEventListener("close", () => {
      dlg.remove();
      resolve(result ? (field ? field.value : true) : (field ? null : false));
    });
    dlg.showModal();
    (field || ok).focus();
    if (field) field.select();
  });
}

export const confirmDialog = ({ title, body, confirm = "OK", danger = false }) =>
  open({ title, body, confirm, danger });

// Resolves to the entered text, or null if cancelled.
export const promptDialog = ({ title, body, confirm = "OK", value = "", placeholder = "", list = "" }) =>
  open({ title, body, confirm, input: { value, placeholder, list } });
