// Sends the E-REDES "aat" cookie to the Home Assistant webhook whenever it
// changes (i.e. right after you log into the Balcão Digital yourself).
const PORTAL = "https://balcaodigital.e-redes.pt";

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

async function sendToken(token, { force = false } = {}) {
  const { webhookUrl } = await chrome.storage.sync.get({ webhookUrl: "" });
  if (!webhookUrl) {
    setBadge("CFG", "#b45309");
    return { ok: false, msg: "Configura o URL do webhook nas opções da extensão." };
  }
  const { lastSent } = await chrome.storage.local.get("lastSent");
  if (!force && lastSent === token) return { ok: true, msg: "Este token já tinha sido enviado." };

  let result;
  try {
    const res = await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok && body.ok !== false) {
      await chrome.storage.local.set({ lastSent: token });
      result = { ok: true, msg: body.status === "unchanged" ? "O HA já tinha este token." : "Token enviado. O HA vai importar os dados em falta." };
      setBadge("OK", "#15803d");
    } else {
      result = { ok: false, msg: `O HA recusou o token (${res.status} ${body.error || ""}).` };
      setBadge("ERR", "#b91c1c");
    }
  } catch (e) {
    result = { ok: false, msg: `Não consegui contactar o HA: ${e.message}` };
    setBadge("ERR", "#b91c1c");
  }
  await chrome.storage.local.set({ lastResult: { ...result, at: Date.now() } });
  return result;
}

chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (removed || cookie.name !== "aat" || !cookie.domain.endsWith("e-redes.pt")) return;
  sendToken(cookie.value);
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type !== "sendNow") return false;
  chrome.cookies
    .get({ url: PORTAL, name: "aat" })
    .then((c) => (c ? sendToken(c.value, { force: true }) : { ok: false, msg: "Não há sessão ativa. Faz login no Balcão Digital primeiro." }))
    .then(reply);
  return true;
});
