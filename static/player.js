const byId = (id) => document.getElementById(id);
const audio = byId("audio");
const list = byId("track-list");
const seek = byId("seek");
const volume = byId("volume");
const importButton = byId("import-button");
const videoUrl = byId("video-url");
const modeButtons = document.querySelectorAll("[data-mode]");

let tracks = [];
let queue = [];
let currentId = null;
let movingTrack = false;
let libraryRequest = 0;
const savedMode = localStorage.getItem("playbackMode");
let mode = Array.from(modeButtons).some((button) => button.dataset.mode === savedMode) ? savedMode : "ordered";

function icon(name) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  const use = document.createElementNS(namespace, "use");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function textElement(tag, className, value) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}

function formatTime(value) {
  const seconds = Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function showStatus(id, message, tone = "info") {
  const status = byId(id);
  status.dataset.tone = tone;
  status.hidden = !message;
  if (status.textContent !== message) status.textContent = message;
}

function setCover(element, track) {
  element.replaceChildren(icon("music"));
  if (!track?.cover?.startsWith("https://")) return;

  const image = document.createElement("img");
  image.alt = "";
  image.loading = "lazy";
  image.referrerPolicy = "no-referrer";
  image.addEventListener("error", () => image.remove(), { once: true });
  image.src = track.cover;
  element.append(image);
}

async function api(path, options) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const result = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(result?.error ?? "本地服务暂时无法处理请求，请重试。");
    error.status = response.status;
    throw error;
  }
  return result;
}

function shuffled(ids) {
  const order = [...ids];
  for (let index = order.length - 1; index > 0; index -= 1) {
    const randomIndex = Math.floor(Math.random() * (index + 1));
    [order[index], order[randomIndex]] = [order[randomIndex], order[index]];
  }
  return order;
}

function rebuildQueue(firstId = null) {
  const ids = tracks.map((track) => track.id);
  if (mode !== "shuffle") {
    queue = ids;
    return;
  }

  // Keep the current track at the head when starting a fresh shuffle run.
  queue = firstId ? [firstId, ...shuffled(ids.filter((id) => id !== firstId))] : shuffled(ids);
}

function renderLibrary() {
  const rows = tracks.map((track, index) => {
    const row = document.createElement("li");
    row.className = "track";
    row.dataset.id = track.id;

    const name = textElement("button", "track-name", "");
    name.type = "button";
    name.title = track.title;
    const cover = textElement("span", "cover", "");
    setCover(cover, track);
    name.append(cover, textElement("span", "track-title", track.title));
    name.addEventListener("click", () => {
      if (track.id === currentId) {
        togglePlay();
        return;
      }
      if (mode === "shuffle") rebuildQueue(track.id);
      selectTrack(track.id, true);
    });

    const actions = textElement("div", "track-actions", "");
    for (const [direction, offset, label] of [["up", -1, "上移"], ["down", 1, "下移"]]) {
      const targetIndex = index + offset;
      const move = textElement("button", "icon-button", "");
      move.type = "button";
      move.dataset.move = direction;
      move.title = label;
      move.disabled = targetIndex < 0 || targetIndex >= tracks.length;
      move.setAttribute("aria-label", `${label}：${track.title}`);
      move.append(icon(direction));
      move.addEventListener("click", () => changeTrackPosition(track, targetIndex + 1));
      actions.append(move);
    }

    const source = textElement("a", "icon-button", "");
    source.href = track.source;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    source.title = "打开原视频";
    source.setAttribute("aria-label", `打开原视频：${track.title}`);
    source.append(icon("external"));

    const remove = textElement("button", "icon-button", "");
    remove.type = "button";
    remove.title = "从歌单删除";
    remove.setAttribute("aria-label", `删除：${track.title}`);
    remove.append(icon("trash"));
    remove.addEventListener("click", () => removeTrack(track, remove));
    actions.append(source, remove);

    row.append(textElement("span", "track-index", String(index + 1)), name,
      textElement("span", "track-artist", track.artist),
      textElement("span", "track-duration", formatTime(track.duration)), actions);
    return row;
  });

  list.replaceChildren(...rows);
  list.hidden = tracks.length === 0;
  byId("empty-state").hidden = !list.hidden;
  byId("track-count").textContent = `${tracks.length} 首歌曲`;
}

function updateControls() {
  const position = queue.indexOf(currentId);
  const playing = currentId !== null && !audio.paused && !audio.ended;
  const play = byId("play");
  play.disabled = currentId === null;
  play.setAttribute("aria-label", playing ? "暂停" : "播放");
  play.title = playing ? "暂停" : "播放";
  play.replaceChildren(icon(playing ? "pause" : "play"));
  byId("previous").disabled = position <= 0;
  byId("next").disabled = position < 0 || position >= queue.length - 1;

  for (const row of list.children) {
    row.setAttribute("aria-current", String(row.dataset.id === currentId));
  }
}

function updateTime() {
  const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
  seek.disabled = duration <= 0;
  seek.max = String(duration || 1);
  seek.value = String(audio.currentTime);
  seek.setAttribute("aria-valuetext", formatTime(audio.currentTime));
  byId("elapsed").textContent = formatTime(audio.currentTime);
  byId("duration").textContent = formatTime(duration);
}

function selectTrack(id, autoplay = false) {
  const track = tracks.find((item) => item.id === id);
  currentId = track?.id ?? null;
  audio.pause();
  if (track) {
    audio.src = track.audio;
  } else {
    audio.removeAttribute("src");
  }
  audio.load();
  setCover(byId("now-cover"), track);
  byId("now-title").textContent = track?.title ?? "未选择歌曲";
  byId("now-artist").textContent = track?.artist ?? "从歌单中选择一首歌";
  showStatus("player-status", "");
  updateControls();
  updateTime();
  if (track && autoplay) playCurrent();
}

async function refreshTracks() {
  const requestId = ++libraryRequest;
  const updatedTracks = await api("/api/tracks");
  if (requestId !== libraryRequest) return;

  tracks = updatedTracks;
  if (mode !== "shuffle" || queue.length === 0) {
    rebuildQueue();
  } else {
    const ids = new Set(tracks.map((track) => track.id));
    queue = queue.filter((id) => ids.has(id));
    const queued = new Set(queue);
    queue.push(...shuffled([...ids].filter((id) => !queued.has(id))));
  }

  const focusedMove = document.activeElement.closest(".track button[data-move]");
  const focusedTrackId = focusedMove?.closest(".track").dataset.id;
  renderLibrary();
  if (focusedMove) {
    const row = list.querySelector(`.track[data-id="${CSS.escape(focusedTrackId)}"]`);
    const button = row?.querySelector(`[data-move="${focusedMove.dataset.move}"]:enabled`)
      ?? row?.querySelector("[data-move]:enabled") ?? row?.querySelector(".track-name");
    button?.focus();
  }
  if (currentId && queue.includes(currentId)) {
    updateControls();
  } else {
    selectTrack(queue[0]);
  }
}

async function playCurrent() {
  try {
    if (audio.ended) audio.currentTime = 0;
    await audio.play();
    showStatus("player-status", "");
  } catch (error) {
    if (error.name === "AbortError") return;
    const message = error.name === "NotAllowedError" ? "浏览器暂停了自动播放，请点击播放。" : "音频无法播放，请尝试重新导入。";
    showStatus("player-status", message, "error");
  }
}

function togglePlay() {
  if (!currentId) return;
  if (audio.paused || audio.ended) {
    playCurrent();
  } else {
    audio.pause();
  }
}

function moveTrack(offset) {
  const position = queue.indexOf(currentId);
  const target = position >= 0 ? queue[position + offset] : null;
  if (target) selectTrack(target, true);
}

async function changeTrackPosition(track, position) {
  if (movingTrack) return;
  movingTrack = true;
  try {
    await api(`/api/tracks/${track.id}/position`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position }),
    });
    await refreshTracks();
    byId("order-status").textContent = `「${track.title}」已移到第 ${position} 位。`;
  } catch (error) {
    showStatus("player-status", `无法调整歌曲位置。${error.message}`, "error");
  } finally {
    movingTrack = false;
  }
}

async function removeTrack(track, button) {
  if (!window.confirm(`删除「${track.title}」及其本地音频？`)) return;
  button.disabled = true;
  const wasCurrent = track.id === currentId;
  if (wasCurrent) selectTrack(null);
  try {
    await api(`/api/tracks/${track.id}`, { method: "DELETE" });
    await refreshTracks();
  } catch (error) {
    if (wasCurrent && !currentId) selectTrack(track.id);
    showStatus("player-status", error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function setImportBusy(busy) {
  importButton.disabled = busy;
  videoUrl.readOnly = busy;
  importButton.querySelector("span").textContent = busy ? "导入中" : "导入音频";
}

async function watchImport(job) {
  setImportBusy(true);
  try {
    while (job.status === "running") {
      const message = job.percent === null ? "正在读取视频音轨…" : `正在导入音频 ${job.percent}%`;
      showStatus("import-status", message);
      await new Promise((resolve) => setTimeout(resolve, 800));
      job = await api("/api/import");
    }

    if (job.status === "done") {
      await refreshTracks();
      videoUrl.value = "";
      showStatus("import-status", job.duplicate ? "这首歌已在歌单中。" : "音频已添加到歌单。");
    } else {
      showStatus("import-status", job.error ?? "导入已中断，请重新提交链接。", "error");
    }
  } catch (error) {
    showStatus("import-status", `无法读取导入状态。${error.message}`, "error");
  } finally {
    setImportBusy(false);
  }
}

byId("import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (importButton.disabled) return;
  setImportBusy(true);
  try {
    const job = await api("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: videoUrl.value }),
    });
    await watchImport(job);
  } catch (error) {
    if (error.status === 409) {
      try {
        await watchImport(await api("/api/import"));
      } catch (statusError) {
        showStatus("import-status", statusError.message, "error");
      }
    } else {
      showStatus("import-status", error.message, "error");
    }
  } finally {
    setImportBusy(false);
  }
});

function renderMode() {
  audio.loop = mode === "repeat-one";
  for (const button of modeButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  }
}

for (const button of modeButtons) {
  button.addEventListener("click", () => {
    if (mode === button.dataset.mode) return;
    mode = button.dataset.mode;
    localStorage.setItem("playbackMode", mode);
    rebuildQueue(currentId);
    renderMode();
    updateControls();
  });
}

byId("play").addEventListener("click", togglePlay);
byId("previous").addEventListener("click", () => moveTrack(-1));
byId("next").addEventListener("click", () => moveTrack(1));
audio.addEventListener("ended", () => moveTrack(1));
for (const event of ["play", "pause", "ended"]) audio.addEventListener(event, updateControls);
for (const event of ["loadedmetadata", "durationchange", "timeupdate", "emptied"]) audio.addEventListener(event, updateTime);
audio.addEventListener("error", () => {
  if (currentId) showStatus("player-status", "音频文件无法读取，请尝试重新导入。", "error");
});

seek.addEventListener("input", () => {
  audio.currentTime = Number(seek.value);
});

const savedVolume = localStorage.getItem("volume");
if (savedVolume !== null && Number(savedVolume) >= 0 && Number(savedVolume) <= 1) volume.value = savedVolume;
audio.volume = Number(volume.value);
volume.addEventListener("input", () => {
  audio.volume = Number(volume.value);
  audio.muted = false;
  localStorage.setItem("volume", volume.value);
});

byId("mute").addEventListener("click", () => {
  audio.muted = !audio.muted;
});

function updateVolume() {
  const mute = byId("mute");
  mute.setAttribute("aria-pressed", String(audio.muted));
  mute.setAttribute("aria-label", audio.muted ? "取消静音" : "静音");
  mute.title = audio.muted ? "取消静音" : "静音";
  mute.replaceChildren(icon(audio.muted || audio.volume === 0 ? "mute" : "volume"));
}

audio.addEventListener("volumechange", updateVolume);
updateVolume();
renderMode();
setImportBusy(true);
try {
  await refreshTracks();
  const job = await api("/api/import");
  if (job.status === "running") await watchImport(job);
} catch (error) {
  showStatus("player-status", error.message, "error");
} finally {
  setImportBusy(false);
}
