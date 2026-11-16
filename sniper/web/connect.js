"use strict";
const tg = window.Telegram?.WebApp;
const el = (id) => document.getElementById(id);
let attempt = null;
let timer = null;
let busy = false;
async function api(action, payload = {}) {
  const response = await fetch(`/connect/api/${action}`, {
    method: "POST", cache: "no-store", credentials: "omit",
    headers: {"Content-Type": "application/json", "X-Telegram-Init-Data": tg.initData},
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Запрос не выполнен.");
  return data;
}
function showError(error) {el("error").textContent = error.message; el("error").hidden = false;}
function render(data) {
  attempt = data;
  el("status").textContent = data.message;
  el("qr-box").hidden = data.stage !== "qr";
  el("password-form").hidden = data.stage !== "password";
  el("cancel").hidden = !["connecting", "qr", "password"].includes(data.stage);
  el("start").hidden = ["connecting", "qr", "password", "done"].includes(data.stage);
  el("close").hidden = data.stage !== "done";
  if (data.stage === "qr") {
    el("qr").src = "data:image/png;base64," + data.qr_png;
    el("expiry").textContent = `Код обновится автоматически. Осталось ${Math.max(0, Math.ceil(data.qr_expires - Date.now()/1000))} сек.`;
  } else {el("qr").removeAttribute("src");}
  if (["error", "expired"].includes(data.stage)) el("start").textContent = "Начать заново";
  if (data.stage !== "password") el("password").value = "";
}
async function poll() {
  clearTimeout(timer);
  if (!attempt || !["connecting", "qr", "password"].includes(attempt.stage)) return;
  if (!busy) {
    try {render(await api("status", {id: attempt.id}));}
    catch (error) {showError(error);}
  }
  if (attempt && ["connecting", "qr", "password"].includes(attempt.stage)) timer = setTimeout(poll, 2000);
}
el("start").addEventListener("click", async () => {
  if (busy) return;
  busy = true; el("start").disabled = true; el("error").hidden = true;
  try {render(await api("start"));}
  catch (error) {showError(error);}
  finally {busy = false; el("start").disabled = false; poll();}
});
el("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !attempt) return;
  busy = true; el("submit-password").disabled = true; el("error").hidden = true;
  let password = el("password").value;
  el("password").value = "";
  try {render(await api("password", {id: attempt.id, password}));}
  catch (error) {showError(error);}
  finally {password = null; busy = false; el("submit-password").disabled = false; poll();}
});
el("cancel").addEventListener("click", async () => {
  if (busy || !attempt) return;
  busy = true;
  try {await api("cancel", {id: attempt.id}); render({...attempt, stage: "expired", message: "Вход отменён."});}
  catch (error) {showError(error);}
  finally {busy = false;}
});
el("close").addEventListener("click", () => tg.close());
if (tg?.initData) {
  tg.ready(); tg.expand();
  el("status").textContent = "Нажми кнопку, когда будешь готов отсканировать QR с телефона.";
  el("start").disabled = false;
}
window.addEventListener("pagehide", () => {clearTimeout(timer); el("password").value = ""; el("qr").removeAttribute("src");});
