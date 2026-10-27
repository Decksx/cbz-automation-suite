// Dashboard shell (M2): who is signed in, server facts, and the effective
// configuration. Real dashboard cards arrive with M6 and M7.
import { api, loadSession } from "./api.js";
import { h } from "./h.js";

const $ = (id) => document.getElementById(id);

function announce(text) {
  $("live").textContent = text;
}

function showNotice(text) {
  const notice = $("notice");
  notice.textContent = text;
  notice.hidden = false;
}

function renderServer(session) {
  const facts = [
    ["Operator", session.operator],
    ["Server version", session.server_version],
    ["Git commit", session.git_sha || "unknown"],
    ["Started (UTC)", session.started_at],
  ];
  $("server").replaceChildren(...facts.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

function renderConfig(values) {
  const rows = Object.entries(values).map(([key, s]) =>
    h("tr", {}, h("td", {}, key), h("td", {}, JSON.stringify(s.value)), h("td", {}, s.source)));
  $("config").replaceChildren(...rows);
}

async function start() {
  try {
    const session = await loadSession();
    const who = $("who");
    who.textContent = `Signed in: ${session.operator}`;
    who.classList.add("ok");
    renderServer(session);
    renderConfig((await api("GET", "/api/config")).values);
    announce("Console ready");
  } catch (err) {
    $("who").textContent = "Not signed in";
    showNotice(err.status === 401
      ? "This page needs a fresh sign-in. Open the console from the CBZ Console shortcut."
      : `The console could not load: ${err.message}`);
  }
}

start();
