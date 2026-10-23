const box = document.getElementById("urls");
const msg = document.getElementById("msg");

chrome.storage.sync.get({ webhookUrls: [], webhookUrl: "" }).then(({ webhookUrls, webhookUrl }) => {
  box.value = (webhookUrls.length ? webhookUrls : webhookUrl ? [webhookUrl] : []).join("\n");
});

document.getElementById("save").onclick = async () => {
  const lines = box.value.split("\n").map((l) => l.trim()).filter(Boolean);
  const urls = [];
  for (const line of lines) {
    try {
      const u = new URL(line);
      if (!/^https?:$/.test(u.protocol) || !u.pathname.includes("/api/webhook/")) throw new Error();
      urls.push(u.href);
    } catch {
      msg.textContent = `URL inválido: ${line}\nTem de ser http(s)://…/api/webhook/<id>.`;
      return;
    }
  }
  if (!urls.length) {
    msg.textContent = "Cola pelo menos um URL.";
    return;
  }
  const origins = [...new Set(urls.map((u) => `${new URL(u).origin}/*`))];
  const granted = await chrome.permissions.request({ origins });
  if (!granted) {
    msg.textContent = "Sem autorização para contactar esses endereços.";
    return;
  }
  await chrome.storage.sync.set({ webhookUrls: urls, webhookUrl: "" });
  await chrome.storage.local.remove(["lastSent", "sentTo"]);
  msg.textContent = `Guardado (${urls.length} Home Assistant). Faz login no Balcão Digital para enviar o token.`;
};
