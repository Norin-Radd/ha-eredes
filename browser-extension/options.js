const input = document.getElementById("url");
const msg = document.getElementById("msg");

chrome.storage.sync.get({ webhookUrl: "" }).then(({ webhookUrl }) => (input.value = webhookUrl));

document.getElementById("save").onclick = async () => {
  let url;
  try {
    url = new URL(input.value.trim());
    if (!/^https?:$/.test(url.protocol) || !url.pathname.includes("/api/webhook/")) throw new Error();
  } catch {
    msg.textContent = "URL inválido: tem de ser http(s)://…/api/webhook/<id>.";
    return;
  }
  const granted = await chrome.permissions.request({ origins: [`${url.origin}/*`] });
  if (!granted) {
    msg.textContent = "Sem autorização para contactar esse endereço.";
    return;
  }
  await chrome.storage.sync.set({ webhookUrl: url.href });
  await chrome.storage.local.remove("lastSent");
  msg.textContent = "Guardado. Faz login no Balcão Digital para enviar o primeiro token.";
};
