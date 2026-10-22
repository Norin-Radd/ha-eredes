const status = document.getElementById("status");

async function showLast() {
  const { lastResult } = await chrome.storage.local.get("lastResult");
  status.textContent = lastResult
    ? `${lastResult.msg} (${new Date(lastResult.at).toLocaleString("pt-PT")})`
    : "Ainda não foi enviado nenhum token.";
}

document.getElementById("login").onclick = () =>
  chrome.tabs.create({ url: "https://balcaodigital.e-redes.pt/" });

document.getElementById("send").onclick = async () => {
  status.textContent = "A enviar…";
  const r = await chrome.runtime.sendMessage({ type: "sendNow" });
  status.textContent = r?.msg || "Sem resposta.";
};

showLast();
