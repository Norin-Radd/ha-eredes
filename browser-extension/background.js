// Sends the E-REDES "aat" cookie to one or more Home Assistant webhooks
// whenever it changes (i.e. right after you log into the Balcão Digital).
const PORTAL = "https://balcaodigital.e-redes.pt";

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
}

async function getUrls() {
  const { webhookUrls, webhookUrl } = await chrome.storage.sync.get({ webhookUrls: [], webhookUrl: "" });
  // Older versions stored a single URL.
  return webhookUrls.length ? webhookUrls : webhookUrl ? [webhookUrl] : [];
}

async function postOne(url, token) {
  const host = new URL(url).host;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok && body.ok !== false) {
      return { host, ok: true, msg: body.status === "unchanged" ? "já tinha este token" : "token aceite" };
    }
    const why = body.error === "invalid_token" ? "token não serve para este CPE" : `${res.status} ${body.error || ""}`.trim();
    return { host, ok: false, msg: `recusado (${why})` };
  } catch (e) {
    return { host, ok: false, msg: `sem ligação (${e.message})` };
  }
}

async function sendToken(token, { force = false } = {}) {
  const urls = await getUrls();
  if (!urls.length) {
    setBadge("CFG", "#b45309");
    return { ok: false, msg: "Configura os URLs dos webhooks nas opções da extensão." };
  }
  const { sentTo = {} } = await chrome.storage.local.get("sentTo");
  const todo = force ? urls : urls.filter((u) => sentTo[u] !== token);
  if (!todo.length) return { ok: true, msg: "Este token já tinha sido enviado para todos." };

  const results = await Promise.all(todo.map((u) => postOne(u, token)));
  todo.forEach((u, i) => { if (results[i].ok) sentTo[u] = token; });

  const okCount = results.filter((r) => r.ok).length;
  setBadge(okCount === results.length ? "OK" : okCount ? `${okCount}/${results.length}` : "ERR",
    okCount === results.length ? "#15803d" : okCount ? "#b45309" : "#b91c1c");

  const result = {
    ok: okCount === results.length,
    msg: results.map((r) => `${r.host}: ${r.msg}`).join("\n"),
  };
  await chrome.storage.local.set({ sentTo, lastResult: { ...result, at: Date.now() } });
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
