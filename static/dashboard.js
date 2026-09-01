const tbody = document.getElementById("leagues-tbody");
const toastContainer = document.getElementById("toast-container");
const logPanel = document.getElementById("log-panel");
const logPanelBackdrop = document.getElementById("log-panel-backdrop");
const logPanelTitle = document.getElementById("log-panel-title");
const logPanelContent = document.getElementById("log-panel-content");
const statRunning = document.getElementById("stat-running");
const statMatches = document.getElementById("stat-matches");
const statOdds = document.getElementById("stat-odds");

let openLogSlug = null;

function toast(text, kind = "default") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " toast-error" : kind === "success" ? " toast-success" : "");
  el.textContent = text;
  toastContainer.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("fr-FR");
}

function statusPill(league) {
  const status = league.status || {};
  if (league.running) {
    if (status.season_started) {
      return `<span class="pill pill-collecting"><span class="pill-dot"></span>Collecte</span>`;
    }
    return `<span class="pill pill-waiting"><span class="pill-dot"></span>Attente round #1</span>`;
  }
  return `<span class="pill pill-stopped"><span class="pill-dot"></span>Arrêté</span>`;
}

function roundProgress(league) {
  const liveRound = league.live_round;
  const status = league.status || {};
  const roundNum = liveRound ? liveRound.round_number : status.current_round;
  const length = league.season_length;

  if (roundNum == null) {
    return `<span class="round-numbers">—</span>`;
  }
  if (!length) {
    return `<span class="round-numbers"><strong>#${roundNum}</strong></span>`;
  }
  const pct = Math.max(2, Math.min(100, Math.round((roundNum / length) * 100)));
  const near = roundNum / length >= 0.85;
  return `
    <div class="round-numbers"><strong>#${roundNum}</strong><span>/ ${length}</span></div>
    <div class="progress-track"><div class="progress-fill${near ? " near-end" : ""}" style="width:${pct}%"></div></div>
  `;
}

function renderRow(league) {
  const tr = document.createElement("tr");
  const status = league.status || {};
  const seasonText = status.season_started
    ? `season_id ${status.season_id}`
    : "—";
  const lastCycle = status.updated_at ? fmtTime(status.updated_at) : "—";
  const errorText = status.last_error;

  tr.innerHTML = `
    <td>
      <span class="league-name">${league.slug}</span>
      <span class="league-id">#${league.entry_point_id}</span>
    </td>
    <td>${statusPill(league)}</td>
    <td>${seasonText}</td>
    <td class="round-cell">${roundProgress(league)}</td>
    <td class="matches-cell">${league.matches_total} <span class="with-odds">(${league.matches_with_odds} cotées)</span></td>
    <td class="time-cell">${lastCycle}</td>
    <td class="${errorText ? "error-cell" : "no-error"}" title="${errorText || ""}">${errorText || "—"}</td>
    <td class="actions-cell">
      <button class="btn btn-sm btn-primary" data-action="start" ${league.running ? "disabled" : ""}>▶</button>
      <button class="btn btn-sm btn-danger-outline" data-action="stop" ${!league.running ? "disabled" : ""}>⏹</button>
      <button class="btn btn-sm btn-secondary" data-action="log">Log</button>
      <button class="btn btn-sm btn-secondary" data-action="merge">⇪</button>
    </td>
  `;

  tr.querySelector('[data-action="start"]').onclick = () => startLeague(league.slug);
  tr.querySelector('[data-action="stop"]').onclick = () => stopLeague(league.slug);
  tr.querySelector('[data-action="log"]').onclick = () => openLog(league.slug);
  tr.querySelector('[data-action="merge"]').onclick = () => mergeLeague(league.slug);

  return tr;
}

function renderSummary(leagues) {
  const running = leagues.filter(l => l.running).length;
  const totalMatches = leagues.reduce((sum, l) => sum + (l.matches_total || 0), 0);
  const totalOdds = leagues.reduce((sum, l) => sum + (l.matches_with_odds || 0), 0);
  statRunning.textContent = `${running}/${leagues.length}`;
  statMatches.textContent = totalMatches.toLocaleString("fr-FR");
  statOdds.textContent = totalOdds.toLocaleString("fr-FR");
}

async function refreshLeagues() {
  try {
    const res = await fetch("/api/leagues");
    const leagues = await res.json();
    tbody.innerHTML = "";
    leagues.forEach(l => tbody.appendChild(renderRow(l)));
    renderSummary(leagues);
  } catch (e) {
    toast("Erreur de chargement du tableau", "error");
  }
}

async function startLeague(slug) {
  const res = await fetch(`/api/leagues/${slug}/start`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) { toast(`${slug} : ${data.error}`, "error"); } else { toast(`${slug} démarré (pid ${data.pid})`, "success"); }
  refreshLeagues();
}

async function stopLeague(slug) {
  const res = await fetch(`/api/leagues/${slug}/stop`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) { toast(`${slug} : ${data.error}`, "error"); } else { toast(`${slug} arrêté`); }
  refreshLeagues();
}

async function mergeLeague(slug) {
  const res = await fetch(`/api/merge?scope=${slug}`, { method: "POST" });
  const data = await res.json();
  toast(res.ok ? `${slug} : dataset.csv régénéré (${data.rows} lignes)` : `Erreur : ${data.error}`, res.ok ? "success" : "error");
}

async function openLog(slug) {
  openLogSlug = slug;
  logPanelTitle.textContent = `Log — ${slug}`;
  logPanel.classList.remove("hidden");
  logPanelBackdrop.classList.remove("hidden");
  await refreshLog();
}

function closeLog() {
  openLogSlug = null;
  logPanel.classList.add("hidden");
  logPanelBackdrop.classList.add("hidden");
}

async function refreshLog() {
  if (!openLogSlug) return;
  const res = await fetch(`/api/leagues/${openLogSlug}/log?lines=300`);
  const data = await res.json();
  const lines = data.lines || [];
  logPanelContent.textContent = lines.length ? lines.join("\n") : "(pas encore de log)";
  logPanelContent.scrollTop = logPanelContent.scrollHeight;
}

document.getElementById("log-panel-close").onclick = closeLog;
logPanelBackdrop.onclick = closeLog;

document.getElementById("btn-start-all").onclick = async () => {
  const res = await fetch("/api/leagues");
  const leagues = await res.json();
  const toStart = leagues.filter(l => !l.running);
  for (const l of toStart) await startLeague(l.slug);
  toast(`${toStart.length} ligue(s) démarrée(s)`, "success");
};

document.getElementById("btn-stop-all").onclick = async () => {
  const res = await fetch("/api/leagues");
  const leagues = await res.json();
  const toStop = leagues.filter(l => l.running);
  for (const l of toStop) await stopLeague(l.slug);
  toast(`${toStop.length} ligue(s) arrêtée(s)`);
};

document.getElementById("btn-merge-all").onclick = async () => {
  toast("Fusion en cours…");
  const res = await fetch("/api/merge?scope=all", { method: "POST" });
  const data = await res.json();
  toast(res.ok ? `dataset_global.csv régénéré (${data.total_rows} lignes)` : `Erreur : ${data.error}`, res.ok ? "success" : "error");
};

refreshLeagues();
setInterval(refreshLeagues, 5000);
setInterval(() => { if (openLogSlug) refreshLog(); }, 3000);
