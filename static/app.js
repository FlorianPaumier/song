const $ = (selector) => document.querySelector(selector);
const allView = new URLSearchParams(location.search).get("view") === "all";
const state = { tracks: [], longProjects: [], supportsLong: false, favorite: false, groupBatches: localStorage.getItem("groupBatches") !== "false", expandedBatches: new Set(), selected: null, selectedIds: new Set(), selectedCompletedIds: new Set(), paused: false, page: 1, pages: 1, total: 0 };
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: {"Content-Type":"application/json", ...(options.headers || {})} });
  if (!response.ok) {
    let detail = `Erreur ${response.status}`;
    try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : detail; } catch (_) {}
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

let toastTimer;
function toast(message) {
  const element = $("#toast"); element.textContent = message; element.classList.add("visible");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => element.classList.remove("visible"), 4200);
}

function actionDialog({title, message, confirmLabel = "Confirmer", inputValue = null}) {
  const dialog = $("#action-dialog");
  $("#dialog-title").textContent = title;
  $("#dialog-message").textContent = message;
  $("#dialog-confirm").textContent = confirmLabel;
  const inputWrap = $("#dialog-input-wrap");
  const input = $("#dialog-input");
  inputWrap.hidden = inputValue === null;
  input.required = inputValue !== null;
  input.value = inputValue ?? "";
  dialog.returnValue = "cancel";
  dialog.showModal();
  if (inputValue !== null) { input.focus(); input.select(); }
  return new Promise((resolve) => dialog.addEventListener("close", () => {
    if (dialog.returnValue !== "confirm") resolve(null);
    else resolve(inputValue === null ? true : input.value.trim());
  }, {once:true}));
}

function formData() {
  return {description: $("#description").value.trim(), lyrics: $("#lyrics").value.trim(),
    title: $("#title").value.trim(), count: Number($("#count").value), duration: Number($("#duration").value),
    vocal_mode: document.querySelector("input[name=vocal_mode]:checked").value,
    lyric_language: $("#lyric-language").value, negative_prompt: $("#negative-prompt").value.trim(),
    generate_covers: $("#generate-covers").checked, long_mode: $("#long-mode").checked,
    total_duration: Number($("#long-duration").value), evolution: $("#long-evolution").value};
}

let previewTimer;
function updatePreview() {
  const data = formData();
  if (data.long_mode) {
    const segments = Math.ceil(data.total_duration / 300);
    $("#create-button").textContent = `Créer un mix de ${data.total_duration / 3600 >= 1 ? `${data.total_duration / 3600} h` : `${data.total_duration / 60} min`} ↗`;
    $("#preview").innerHTML = data.description.length >= 3
      ? `<strong>Format long instrumental</strong><br>${data.total_duration / 60} min · ${segments} segments · évolution ${escapeHtml(data.evolution)}`
      : "Renseigne une description pour commencer.";
    return;
  }
  $("#create-button").textContent = `Ajouter ${data.count || 0} chanson${data.count === 1 ? "" : "s"} à la file ↗`;
  if (!data.description || data.description.length < 3) { $("#preview").textContent = "Renseigne une description pour commencer."; return; }
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    try {
      const result = await request("/api/preview", {method:"POST", body:JSON.stringify(data)});
      const outline = result.outline?.length ? ` · Structure : ${result.outline.join(" → ")}` : "";
      const voice = result.vocal_mode === "instrumental" ? "Instrumental — aucune parole" : (result.lyrics_generated ? "Paroles générées" : "Paroles fournies");
      const language = result.vocal_mode === "instrumental" ? "" : ` · ${$("#lyric-language").selectedOptions[0].textContent}`;
      $("#preview").innerHTML = `<strong>${escapeHtml(result.title)}</strong><br>${result.bpm ? `${result.bpm} BPM · ` : ""}${data.duration / 60} min · ${voice}${escapeHtml(language)}${escapeHtml(outline)}<br><span title="${escapeHtml(result.negative_prompt)}">Exclusions : ${escapeHtml(result.negative_prompt)}</span>`;
    } catch (error) { $("#preview").textContent = error.message; }
  }, 350);
}

function statusLabel(status) {
  return {draft:"À préparer",preparing:"Préparation",ready:"Prêt",submitting:"Envoi",generating:"Génération",assembling:"Assemblage",post_processing:"Finalisation",queued:"En attente",running:"Génération",completed:"Terminé",failed:"Échec",canceled:"Annulé"}[status] || status;
}

function progressLabel(track) {
  if (!["preparing","submitting","generating","post_processing","running"].includes(track.status)) return "";
  if (track.status === "preparing") return "Génération et validation des paroles…";
  if (track.status === "submitting") return "Envoi à ACE-Step…";
  if (track.status === "post_processing") return "Téléchargement et finalisation…";
  if (!track.last_checked_at) return "Envoi à ACE-Step…";
  const checked = new Date(track.last_checked_at);
  const time = Number.isNaN(checked.getTime()) ? "" : ` · vérifié à ${checked.toLocaleTimeString("fr", {hour:"2-digit", minute:"2-digit"})}`;
  const stage = track.ace_stage ? ` · ${track.ace_stage}` : "";
  const progress = typeof track.ace_progress === "number" && track.ace_progress >= 0 && track.ace_progress <= 1
    ? ` · ${Math.round(track.ace_progress * 100)} %` : "";
  return `En cours${stage}${progress}${time}`;
}

function trackMarkup(track) {
    const id = encodeURIComponent(track.id);
    const done = track.status === "completed";
    const selectable = allView && !["draft","preparing","ready","submitting","generating","post_processing","queued","running"].includes(track.status) && !["queued","running"].includes(track.cover_status);
    return `<article class="track ${selectable ? "selectable-track" : ""}" data-id="${escapeHtml(track.id)}">
      ${selectable ? `<label class="track-select" title="Sélectionner"><input type="checkbox" data-action="select" ${state.selectedIds.has(track.id) ? "checked" : ""} aria-label="Sélectionner ${escapeHtml(track.title)}"></label>` : ""}
      <button class="track-cover ${track.cover_status === "completed" ? "has-cover" : ""}" type="button" data-action="play" ${done ? "" : "disabled"} aria-label="Écouter ${escapeHtml(track.title)}">${track.cover_status === "completed" ? `<img src="/api/tracks/${id}/cover" alt="">` : (done ? "▶" : "♪")}</button>
      <div class="track-main"><div class="track-head"><strong class="track-title" title="${escapeHtml(track.title)}">${escapeHtml(track.title)}</strong><span class="track-status ${escapeHtml(track.status)}">${statusLabel(track.status)}</span></div>
      <p class="track-desc" title="${escapeHtml(track.description)}">${escapeHtml(track.description)}${track.requested_bpm ? ` · ${track.requested_bpm} BPM` : ""}</p>
      ${progressLabel(track) ? `<p class="track-progress" title="${escapeHtml(progressLabel(track))}">${escapeHtml(progressLabel(track))}</p>` : ""}
      <div class="track-actions"><button type="button" data-action="favorite" class="${track.favorite ? "is-favorite" : ""}" aria-label="${track.favorite ? "Retirer des favoris" : "Ajouter aux favoris"}">${track.favorite ? "★" : "☆"}</button><button type="button" data-action="rename">Renommer</button>${done ? `<a href="/api/tracks/${id}/export?format=mp3">MP3</a><a href="/api/tracks/${id}/export?format=flac">FLAC</a>` : ""}${track.cover_status === "completed" ? `<a href="/api/tracks/${id}/cover?download=true">Pochette</a>` : ""}${done && ["none","failed"].includes(track.cover_status) ? `<button type="button" data-action="cover">Créer pochette</button>` : ""}${track.cover_status === "queued" ? `<span class="cover-state">Pochette en attente</span>` : ""}${track.cover_status === "running" ? `<span class="cover-state">Pochette en cours</span>` : ""}${["failed","canceled"].includes(track.status) ? `<button type="button" data-action="retry">Réessayer</button>` : ""}${["preparing","ready","submitting","generating","post_processing"].includes(track.status) ? `<button type="button" data-action="cancel" class="delete-action">Arrêter</button><button type="button" data-action="force-retry" class="delete-action">Forcer le redémarrage</button>` : ""}${selectable ? `<button type="button" data-action="delete" class="delete-action">Supprimer</button>` : ""}</div>
      </div></article>`;
}

function renderTracks() {
  const tracks = state.tracks;
  $("#track-total").textContent = `${state.total} son${state.total === 1 ? "" : "s"}`;
  const hasGroups = tracks.some((track) => track.batch_size > 2);
  $("#group-batches-option").hidden = !hasGroups;
  $("#group-batches").checked = state.groupBatches;
  if (!tracks.length) {
    $("#tracks").innerHTML = `<div class="empty">Aucun son ici pour le moment.<br>Crée une chanson ou essaie une autre recherche.</div>`;
  } else if (!state.groupBatches || !hasGroups) {
    $("#tracks").innerHTML = tracks.map(trackMarkup).join("");
  } else {
    const renderedBatches = new Set();
    $("#tracks").innerHTML = tracks.map((track) => {
      if (track.batch_size <= 2) return trackMarkup(track);
      if (renderedBatches.has(track.batch_id)) return "";
      renderedBatches.add(track.batch_id);
      const batchTracks = tracks.filter((item) => item.batch_id === track.batch_id);
      const completed = batchTracks.filter((item) => item.status === "completed").length;
      const visible = batchTracks.length === track.batch_size ? `${track.batch_size} morceaux` : `${batchTracks.length}/${track.batch_size} affichés`;
      const title = track.title.replace(/ · \d+$/, "");
      const open = state.expandedBatches.has(track.batch_id) ? " open" : "";
      return `<details class="batch-group" data-batch-id="${escapeHtml(track.batch_id)}"${open}><summary><span class="batch-mark">${track.batch_size}</span><span class="batch-copy"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(visible)} · ${completed} terminé${completed === 1 ? "" : "s"}</small></span><span class="batch-chevron" aria-hidden="true">⌄</span></summary><div class="batch-meta-actions"><button type="button" data-batch-action="details">Détails du lot</button></div><div class="batch-tracks">${batchTracks.map(trackMarkup).join("")}</div></details>`;
    }).join("");
  }
  updateBulkActions();
}

function updateBulkActions() {
  if (!allView) return;
  $("#selected-count").textContent = `${state.selectedIds.size} sélection${state.selectedIds.size === 1 ? "" : "s"}`;
  $("#delete-selected").disabled = state.selectedIds.size === 0;
  $("#download-selected").disabled = state.selectedCompletedIds.size === 0;
}

async function loadTracks() {
  const limit = allView ? 20 : Number($("#recent-limit").value);
  const params = new URLSearchParams({q:$("#search").value.trim(), favorite:String(state.favorite),
    grouped:String(state.groupBatches), limit:String(limit), page:String(allView ? state.page : 1)});
  const data = await request(`/api/tracks?${params}`);
  state.tracks = data.tracks;
  state.total = data.total;
  state.pages = data.pages;
  if (state.page > state.pages) { state.page = state.pages; return loadTracks(); }
  renderTracks();
  if (allView) {
    $("#page-label").textContent = `Page ${state.page} / ${state.pages}`;
    $("#previous-page").disabled = state.page <= 1;
    $("#next-page").disabled = state.page >= state.pages;
  }
  if (state.selected) {
    const updated = data.tracks.find((track) => track.id === state.selected.id);
    if (updated) { state.selected = updated; renderPlayer(); }
  }
}

function formatLongDuration(seconds) {
  const hours = Math.floor(seconds / 3600); const minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours} h${minutes ? ` ${minutes} min` : ""}` : `${minutes} min`;
}

async function loadLongProjects() {
  if (!state.supportsLong) {
    state.longProjects = [];
    $("#long-projects-section").hidden = true;
    return;
  }
  const response = await fetch("/api/long-projects");
  if (!response.ok) throw new Error(`Erreur ${response.status}`);
  const data = await response.json();
  state.longProjects = data.projects;
  $("#long-projects-section").hidden = !data.projects.length;
  $("#long-projects").innerHTML = data.projects.map((project) => {
    const done = project.status === "completed";
    const canFinalize = project.status === "generating" && project.completed_segments === project.segment_count;
    const canDelete = ["completed", "failed"].includes(project.status);
    const progress = `${project.completed_segments}/${project.segment_count} segments`;
    const active = Number(project.active_segments || 0);
    const activeLabel = active ? `<small class="long-active">${active} segment${active === 1 ? "" : "s"} en cours de génération</small>` : "";
    return `<article class="long-project" data-project-id="${escapeHtml(project.id)}"><div><strong>${escapeHtml(project.title)}</strong><small>${formatLongDuration(project.total_duration)} · ${escapeHtml(progress)} · ${escapeHtml(statusLabel(project.status))}</small>${activeLabel}${project.error ? `<small class="long-error">${escapeHtml(project.error)}</small>` : ""}</div><div class="track-actions">${done ? `<button type="button" data-long-action="play">Écouter</button><a href="/api/long-projects/${encodeURIComponent(project.id)}/export?format=mp3">MP3</a><a href="/api/long-projects/${encodeURIComponent(project.id)}/export?format=flac">FLAC</a>` : ""}${project.active_track_id ? `<button type="button" data-long-action="cancel-active" class="delete-action">Arrêter le segment</button>` : ""}${canFinalize ? `<button type="button" data-long-action="finalize">Finaliser maintenant</button>` : ""}${project.status === "failed" ? `<button type="button" data-long-action="retry">Relancer les segments en échec</button>` : ""}${canDelete ? `<button type="button" data-long-action="delete" class="delete-action">Retirer</button>` : ""}</div></article>`;
  }).join("");
}

function renderPlayer() {
  const track = state.selected;
  if (!track) return;
  $("#player-track").innerHTML = `<span class="cover-mini">♪</span><div><strong>${escapeHtml(track.title)}</strong><small>${escapeHtml(track.description)}</small></div>`;
  $("#player-actions").innerHTML = `<a href="/api/tracks/${encodeURIComponent(track.id)}/export?format=mp3">↓ MP3</a>`;
}

async function loadStatus() {
  const data = await request("/api/status");
  state.paused = data.paused;
  state.supportsLong = Array.isArray(data.features) && data.features.includes("long_projects");
  $("#long-mode-option").hidden = !state.supportsLong;
  if (!state.supportsLong && $("#long-mode").checked) {
    $("#long-mode").checked = false;
    $("#long-mode").dispatchEvent(new Event("change"));
  }
  const connection = $("#connection");
  connection.textContent = data.idle ? "ACE-Step en veille" : (data.ace.online ? `ACE-Step connecté · ${data.ace.model || "modèle en chargement"}` : "ACE-Step hors ligne");
  connection.className = `connection ${data.idle ? "" : (data.ace.online ? "online" : "offline")}`;
  const q = data.queue;
  const active = (q.preparing || 0) + (q.submitting || 0) + (q.generating || 0) + (q.post_processing || 0) + (q.running || 0);
  const waiting = (q.draft || 0) + (q.ready || 0) + (q.queued || 0);
  $("#queue-summary").textContent = `${active} en cours · ${waiting} en attente · ${q.completed || 0} terminée${q.completed === 1 ? "" : "s"}${data.paused ? " · en pause" : ""}`;
  $("#pause-queue").textContent = data.paused ? "Reprendre" : "Pause";
  $("#cancel-queue").disabled = !(waiting > 0);
  return {queue:q, covers:data.covers, idle:data.idle};
}

let refreshTimer;
let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  let nextDelay = 60000;
  try {
    const activity = await loadStatus();
    await Promise.all([loadTracks(), loadLongProjects(), checkFinishedBatches()]);
    const activeStatuses = ["draft","preparing","ready","submitting","generating","post_processing","queued","running"];
    if (activeStatuses.some((name) => activity.queue[name]) || activity.covers.running || activity.covers.queued) nextDelay = 15000;
    else nextDelay = null;
  } catch (error) { toast(error.message); nextDelay = 30000; }
  finally {
    refreshing = false;
    clearTimeout(refreshTimer);
    if (nextDelay !== null) refreshTimer = setTimeout(refresh, document.hidden ? Math.max(nextDelay, 60000) : nextDelay);
  }
}

let knownFinished = null;
async function checkFinishedBatches() {
  const data = await request("/api/finished-batches");
  const ids = new Set(data.batches.map((batch) => batch.id));
  if (knownFinished !== null) {
    for (const batch of data.batches) {
      if (knownFinished.has(batch.id)) continue;
      const message = `${batch.completed}/${batch.total} chanson${batch.total === 1 ? "" : "s"} prête${batch.completed === 1 ? "" : "s"}${batch.failed ? ` · ${batch.failed} échec(s)` : ""}`;
      toast(message);
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("Song Studio · lot terminé", {body:message, tag:`song-batch-${batch.id}`});
      }
    }
  }
  knownFinished = ids;
}

$("#notify-toggle").addEventListener("click", async () => {
  if (!("Notification" in window) || !window.isSecureContext) {
    toast("Notifications navigateur indisponibles sur cette adresse HTTP. Utilise le push ntfy ou HTTPS.");
    return;
  }
  const permission = await Notification.requestPermission();
  toast(permission === "granted" ? "Notifications de fin de lot activées." : "Notifications non autorisées.");
});

$("#create-form").addEventListener("input", updatePreview);
document.querySelectorAll("input[name=vocal_mode]").forEach((input) => input.addEventListener("change", () => {
  $("#lyrics-block").hidden = input.value !== "provided" || !input.checked;
  $("#lyric-language").disabled = input.value === "instrumental" && input.checked;
  $("#lyrics").required = input.value === "provided" && input.checked;
  updatePreview();
}));
$("#long-mode").addEventListener("change", () => {
  const enabled = $("#long-mode").checked;
  $("#long-settings").hidden = !enabled;
  $("#vocal-mode-block").hidden = enabled;
  $("#language-block").hidden = enabled;
  $("#lyrics-block").hidden = true;
  $("#normal-duration-block").hidden = enabled;
  $("#batch-controls").hidden = enabled;
  $("#generate-covers").closest("label").hidden = enabled;
  updatePreview();
});
$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formData();
  const button = $("#create-button"); button.disabled = true;
  try {
    if (data.long_mode) {
      const result = await request("/api/long-projects", {method:"POST", body:JSON.stringify({
        description:data.description, title:data.title, total_duration:data.total_duration,
        evolution:data.evolution, negative_prompt:data.negative_prompt
      })});
      toast(`Mix long ajouté : ${result.segments} segments à générer.`);
    } else {
      await request("/api/batches", {method:"POST", body:JSON.stringify(data)});
      toast(`${data.count} chanson${data.count === 1 ? "" : "s"} ajoutée${data.count === 1 ? "" : "s"} à la file.`);
    }
    await refresh();
    if (window.innerWidth < 900) $("[data-panel=library]").click();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

$("#count").addEventListener("input", () => {
  const value = $("#count").value;
  document.querySelectorAll(".count-choice").forEach((button) => button.classList.toggle("active", button.dataset.count === value));
});
document.querySelectorAll(".count-choice").forEach((button) => button.addEventListener("click", () => {
  $("#count").value = button.dataset.count;
  $("#count").dispatchEvent(new Event("input", {bubbles:true}));
}));

let searchTimer;
$("#search").addEventListener("input", () => { state.page = 1; clearTimeout(searchTimer); searchTimer = setTimeout(() => loadTracks().catch((error) => toast(error.message)), 250); });
$("#favorites-filter").addEventListener("click", () => {
  state.favorite = !state.favorite;
  state.page = 1;
  $("#favorites-filter").setAttribute("aria-pressed", String(state.favorite));
  loadTracks().catch((error) => toast(error.message));
});
$("#group-batches").addEventListener("change", async (event) => {
  state.groupBatches = event.target.checked;
  localStorage.setItem("groupBatches", String(state.groupBatches));
  state.page = 1;
  try { await loadTracks(); } catch (error) { toast(error.message); }
});
$("#tracks").addEventListener("toggle", (event) => {
  const group = event.target.closest(".batch-group");
  if (!group) return;
  if (group.open) state.expandedBatches.add(group.dataset.batchId);
  else state.expandedBatches.delete(group.dataset.batchId);
}, true);

async function showBatchDetails(batchId) {
  const tracks = state.tracks.filter((track) => track.batch_id === batchId);
  if (!tracks.length) return;
  const details = await request(`/api/batches/${encodeURIComponent(batchId)}`);
  const first = tracks[0];
  const title = first.title.replace(/ · \d+$/, "");
  const counts = tracks.reduce((result, track) => {
    result[track.status] = (result[track.status] || 0) + 1;
    return result;
  }, {});
  const languages = {en:"English",fr:"Français",de:"Deutsch",es:"Español",it:"Italiano",pt:"Português"};
  const modes = {instrumental:"Instrumental",auto:"Paroles automatiques",provided:"Paroles fournies"};
  const created = new Date(details.created_at.replace(" ", "T") + "Z");
  $("#batch-dialog-title").textContent = title;
  $("#batch-dialog-description").textContent = details.description;
  $("#batch-dialog-stats").innerHTML = [
    ["Morceaux", first.batch_size],
    ["Durée visée", `${Math.round(first.requested_duration / 60 * 10) / 10} min`],
    ["Tempo", first.requested_bpm ? `${first.requested_bpm} BPM` : "Automatique"],
    ["Mode", modes[first.vocal_mode] || first.vocal_mode],
    ["Langue", first.vocal_mode === "instrumental" ? "—" : (languages[first.lyric_language] || first.lyric_language)],
    ["Créé", Number.isNaN(created.getTime()) ? details.created_at : created.toLocaleString("fr", {dateStyle:"medium",timeStyle:"short"})],
  ].map(([label,value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
  $("#batch-dialog-statuses").innerHTML = Object.entries(counts).map(([status,count]) => `<span class="batch-status ${escapeHtml(status)}"><strong>${count}</strong> ${escapeHtml(statusLabel(status))}</span>`).join("");
  $("#batch-dialog").showModal();
}

$("#tracks").addEventListener("click", async (event) => {
  const batchAction = event.target.closest("[data-batch-action]");
  if (batchAction) {
    try { await showBatchDetails(batchAction.closest("[data-batch-id]")?.dataset.batchId); }
    catch (error) { toast(error.message); }
    return;
  }
  const action = event.target.closest("[data-action]");
  if (!action) return;
  const track = state.tracks.find((item) => item.id === action.closest("[data-id]")?.dataset.id);
  if (!track) return;
  try {
    if (action.dataset.action === "select") {
      if (action.checked) {
        state.selectedIds.add(track.id);
        if (track.status === "completed") state.selectedCompletedIds.add(track.id);
      } else {
        state.selectedIds.delete(track.id);
        state.selectedCompletedIds.delete(track.id);
      }
      updateBulkActions();
    } else if (action.dataset.action === "play") {
      state.selected = track;
      $("#audio-player").src = `/api/tracks/${encodeURIComponent(track.id)}/audio`;
      renderPlayer();
      await $("#audio-player").play();
    } else if (action.dataset.action === "favorite") {
      await request(`/api/tracks/${encodeURIComponent(track.id)}`, {method:"PATCH",body:JSON.stringify({favorite:!track.favorite})});
      await loadTracks();
    } else if (action.dataset.action === "rename") {
      const title = await actionDialog({title:"Renommer le morceau", message:"Choisis le titre affiché dans la bibliothèque.", confirmLabel:"Enregistrer", inputValue:track.title});
      if (title) {
        await request(`/api/tracks/${encodeURIComponent(track.id)}`, {method:"PATCH",body:JSON.stringify({title})});
        await loadTracks();
      }
    } else if (action.dataset.action === "retry") {
      await request(`/api/tracks/${encodeURIComponent(track.id)}/retry`, {method:"POST"});
      await refresh();
    } else if (action.dataset.action === "force-retry") {
      const confirmed = await actionDialog({title:"Redémarrer depuis zéro ?", message:"Song Studio abandonnera le suivi de la génération actuelle et recréera entièrement cette chanson. ACE-Step peut continuer l’ancien calcul en arrière-plan car son API ne permet pas de l’annuler.", confirmLabel:"Redémarrer"});
      if (!confirmed) return;
      await request(`/api/tracks/${encodeURIComponent(track.id)}/force-retry`, {method:"POST"});
      toast("La chanson va redémarrer depuis zéro.");
      await refresh();
    } else if (action.dataset.action === "cancel") {
      const confirmed = await actionDialog({title:"Arrêter cette génération ?", message:"La tâche sera annulée dans Song Studio. ACE-Step ne permet pas d’annuler à distance : son calcul éventuel peut continuer sur le serveur.", confirmLabel:"Arrêter"});
      if (!confirmed) return;
      await request(`/api/tracks/${encodeURIComponent(track.id)}/cancel`, {method:"POST"});
      toast("Génération arrêtée dans Song Studio.");
      await refresh();
    } else if (action.dataset.action === "cover") {
      await request(`/api/tracks/${encodeURIComponent(track.id)}/cover/retry`, {method:"POST"});
      toast("Pochette ajoutée à la file image.");
      await loadTracks();
    } else if (action.dataset.action === "delete") {
      const confirmed = await actionDialog({title:"Retirer le morceau ?", message:`« ${track.title} » disparaîtra de la bibliothèque, mais ses fichiers seront conservés.`, confirmLabel:"Retirer"});
      if (!confirmed) return;
      await request(`/api/tracks/${encodeURIComponent(track.id)}`, {method:"DELETE"});
      state.selectedIds.delete(track.id);
      state.selectedCompletedIds.delete(track.id);
      if (state.selected?.id === track.id) {
        $("#audio-player").pause(); $("#audio-player").removeAttribute("src"); state.selected = null;
      }
      toast("Morceau retiré de la bibliothèque. Les fichiers sont conservés.");
      await loadTracks();
    }
  } catch (error) { toast(error.message); }
});

$("#long-projects").addEventListener("click", async (event) => {
  const action = event.target.closest("[data-long-action]");
  if (!action) return;
  const project = state.longProjects.find((item) => item.id === action.closest("[data-project-id]")?.dataset.projectId);
  if (!project) return;
  try {
    if (action.dataset.longAction === "cancel-active") {
      const confirmed = await actionDialog({title:"Arrêter le segment actif ?", message:`${project.active_track_title || "Le segment actif"} sera annulé dans Song Studio. ACE-Step peut continuer son calcul distant.`, confirmLabel:"Arrêter"});
      if (!confirmed) return;
      await request(`/api/tracks/${encodeURIComponent(project.active_track_id)}/cancel`, {method:"POST"});
      toast("Segment arrêté.");
      await refresh();
      return;
    }
    if (action.dataset.longAction === "play") {
      $("#audio-player").src = `/api/long-projects/${encodeURIComponent(project.id)}/audio`;
      $("#player-track").innerHTML = `<span class="cover-mini">♪</span><div><strong>${escapeHtml(project.title)}</strong><small>Mix long · ${formatLongDuration(project.total_duration)}</small></div>`;
      $("#player-actions").innerHTML = `<a href="/api/long-projects/${encodeURIComponent(project.id)}/export?format=mp3">↓ MP3</a>`;
      await $("#audio-player").play();
    } else if (action.dataset.longAction === "retry") {
      const result = await request(`/api/long-projects/${encodeURIComponent(project.id)}/retry`, {method:"POST"});
      toast(`${result.retried} segment${result.retried === 1 ? " relancé" : "s relancés"}.`);
      await refresh();
    } else if (action.dataset.longAction === "finalize") {
      action.disabled = true;
      action.textContent = "Finalisation…";
      await request(`/api/long-projects/${encodeURIComponent(project.id)}/finalize`, {method:"POST"});
      toast("Assemblage final lancé.");
      await refresh();
    } else if (action.dataset.longAction === "delete") {
      const confirmed = await actionDialog({title:"Retirer le mix long ?", message:`« ${project.title} » disparaîtra de la bibliothèque, mais ses fichiers seront conservés.`, confirmLabel:"Retirer"});
      if (!confirmed) return;
      await request(`/api/long-projects/${encodeURIComponent(project.id)}`, {method:"DELETE"});
      toast("Mix long retiré. Les fichiers sont conservés.");
      await loadLongProjects();
    }
  } catch (error) { toast(error.message); }
});

$("#pause-queue").addEventListener("click", async () => {
  try { await request(state.paused ? "/api/queue/resume" : "/api/queue/pause", {method:"POST"}); await loadStatus(); }
  catch (error) { toast(error.message); }
});
let cancelQueueArmed = false;
let cancelQueueTimer;
$("#cancel-queue").addEventListener("click", async () => {
  const button = $("#cancel-queue");
  if (!cancelQueueArmed) {
    cancelQueueArmed = true;
    button.textContent = "Confirmer l’annulation";
    toast("Clique une seconde fois pour annuler toute l’attente. Le morceau actif continuera.");
    clearTimeout(cancelQueueTimer);
    cancelQueueTimer = setTimeout(() => {
      cancelQueueArmed = false;
      button.textContent = "Annuler l’attente";
    }, 6000);
    return;
  }
  cancelQueueArmed = false;
  clearTimeout(cancelQueueTimer);
  button.disabled = true;
  button.textContent = "Annulation…";
  try {
    const result = await request("/api/queue/cancel-pending", {method:"POST"});
    toast(`${result.canceled} chanson${result.canceled === 1 ? " annulée" : "s annulées"}. La génération déjà active continue.`);
    await Promise.all([loadStatus(), loadTracks()]);
  }
  catch (error) { toast(error.message); }
  finally {
    button.textContent = "Annuler l’attente";
  }
});
document.querySelectorAll(".mobile-nav button").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".mobile-nav button").forEach((item) => item.classList.toggle("active", item === button));
  $(".workspace").classList.toggle("show-library", button.dataset.panel === "library");
}));
$("#recent-limit").addEventListener("change", () => loadTracks().catch((error) => toast(error.message)));
$("#previous-page").addEventListener("click", () => { if (state.page > 1) { state.page--; loadTracks(); } });
$("#next-page").addEventListener("click", () => { if (state.page < state.pages) { state.page++; loadTracks(); } });
$("#select-page").addEventListener("click", () => {
  const selectable = state.tracks.filter((track) => !["draft","preparing","ready","submitting","generating","post_processing","queued","running"].includes(track.status) && !["queued","running"].includes(track.cover_status));
  const allSelected = selectable.length && selectable.every((track) => state.selectedIds.has(track.id));
  selectable.forEach((track) => {
    if (allSelected) {
      state.selectedIds.delete(track.id);
      state.selectedCompletedIds.delete(track.id);
    } else {
      state.selectedIds.add(track.id);
      if (track.status === "completed") state.selectedCompletedIds.add(track.id);
    }
  });
  renderTracks();
});
$("#delete-selected").addEventListener("click", async () => {
  const ids = [...state.selectedIds];
  if (!ids.length) return;
  const confirmed = await actionDialog({title:"Retirer la sélection ?", message:`${ids.length} morceau${ids.length === 1 ? " disparaîtra" : "x disparaîtront"} de la bibliothèque, mais ${ids.length === 1 ? "son fichier sera conservé" : "leurs fichiers seront conservés"}.`, confirmLabel:"Retirer"});
  if (!confirmed) return;
  try {
    const result = await request("/api/tracks/delete-bulk", {method:"POST", body:JSON.stringify({ids})});
    if (state.selected && ids.includes(state.selected.id)) {
      $("#audio-player").pause();
      $("#audio-player").removeAttribute("src");
      state.selected = null;
    }
    state.selectedIds.clear();
    state.selectedCompletedIds.clear();
    toast(`${result.deleted} morceau${result.deleted === 1 ? " retiré" : "x retirés"} de la bibliothèque.`);
    await loadTracks();
  } catch (error) { toast(error.message); }
});
$("#download-selected").addEventListener("click", async () => {
  const ids = [...state.selectedIds];
  if (!ids.length) return;
  const button = $("#download-selected");
  button.disabled = true;
  button.textContent = "Préparation…";
  try {
    const response = await fetch("/api/tracks/export-bulk", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({ids, format:$("#bulk-format").value})
    });
    if (!response.ok) {
      let detail = `Erreur ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = "song-studio-export.zip";
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Archive ZIP téléchargée.");
  } catch (error) { toast(error.message); }
  finally { button.textContent = "Télécharger ZIP"; updateBulkActions(); }
});
const savedTheme = localStorage.getItem("song-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
$("#theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next; localStorage.setItem("song-theme", next);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { clearTimeout(refreshTimer); refresh(); }
});
if (allView) {
  document.body.classList.add("library-page");
  $("#library-title").textContent = "Tous les morceaux";
  $("#recent-limit-label").hidden = true;
  $("#pagination").hidden = false;
  $("#bulk-actions").hidden = false;
  $("#library-link").textContent = "← Retour au studio";
  $("#library-link").href = "/";
}
updatePreview(); refresh();
