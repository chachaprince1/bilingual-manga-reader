"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const app = $("#app");
const state = {
  library: [],
  current: null,
  pageIndex: 0,
  displayedIds: [],
  fit: "screen",
  zoom: 1,
  direction: "rtl",
  ocrDisplay: "hover",
  clickNavigation: true,
  libraryFilter: "all",
  librarySort: "recent",
  selectedFiles: [],
  selectedSource: null,
  relinkVolumeId: null,
  conversionJob: null,
  conversionVolumeId: null,
  removal: null,
  pairingReview: null,
  pendingPairings: [],
  reconnectTimer: null,
  mapping: null,
  view: { kind: "library", series: "" },
  libraryScroll: 0,
  seriesScroll: new Map(),
  bulk: {
    scanId: null,
    job: null,
    filter: "all",
    query: "",
    importing: false,
  },
};

const STABLE_PORT = 8765;
const BILINGUAL_MANGA_LINK = '<a href="https://github.com/B-M-dev/Bilingual-Manga-archive" target="_blank" rel="noreferrer">Bilingual Manga data</a>';

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function jsonPost(url, data) {
  return api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function toast(message, kind = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.dataset.kind = kind;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 4200);
}

function volumeName(volume) {
  return [volume.volume_label, volume.chapter_label].filter(Boolean).join(" · ") || volume.title;
}

function contentTypeName(volume) {
  if (volume.content_type === "bilingual") return `Bilingual · ${volume.language === "en" ? "English" : "Japanese"}${volume.mokuro_path ? " · Mokuro" : ""}`;
  if (volume.content_type === "mokuro" || volume.mokuro_path) return "Mokuro · Japanese";
  return volume.language === "en" ? "Raw · English" : "Raw · Japanese";
}

function contentTypeClass(volume) {
  return volume.content_type === "bilingual" ? "bilingual" : (volume.mokuro_path ? "mokuro" : "raw");
}

function progressPercent(volume) {
  if (!volume.progress_page || !volume.page_count) return 0;
  return Math.max(1, Math.round((Number(volume.progress_ordinal ?? 0) + 1) / volume.page_count * 100));
}

function groupLibrary(volumes) {
  const groups = new Map();
  for (const volume of volumes) {
    if (!groups.has(volume.series)) groups.set(volume.series, []);
    groups.get(volume.series).push(volume);
  }
  return [...groups.entries()];
}

function rememberScroll() {
  if (state.view.kind === "library") state.libraryScroll = window.scrollY;
  if (state.view.kind === "series" && state.view.series) state.seriesScroll.set(state.view.series, window.scrollY);
}

function restoreScroll(value) {
  requestAnimationFrame(() => window.scrollTo({ top: Number(value || 0), behavior: "instant" }));
}

function numericLabel(value) {
  const match = String(value || "").match(/(?:volume|vol\.?|chapter|ch\.?|巻|話)?\s*(\d+(?:\.\d+)?)/i);
  return match ? Number(match[1]) : Number.POSITIVE_INFINITY;
}

function compareEditionGroups([labelA], [labelB]) {
  const numberA = numericLabel(labelA);
  const numberB = numericLabel(labelB);
  if (numberA !== numberB) return numberA - numberB;
  return String(labelA).localeCompare(String(labelB), undefined, { numeric: true, sensitivity: "base" });
}

function isBilingual(volumes) {
  return volumes.some(volume => volume.language === "jp" && volume.available) &&
    volumes.some(volume => volume.language === "en" && volume.available);
}

function groupBookmarks(volumes) {
  return volumes.reduce((total, volume) => total + Number(volume.bookmark_count || 0), 0);
}

function seriesCard(series, volumes) {
  const cover = volumes.find(volume => volume.available) || volumes[0];
  const missing = volumes.filter(volume => !volume.available).length;
  const favorite = volumes.some(volume => volume.favorite);
  const resume = volumes.find(volume => volume.progress_page && volume.available);
  const bilingual = isBilingual(volumes);
  const bookmarks = groupBookmarks(volumes);
  return `<article class="series-card" data-series="${escapeHtml(series)}">
    <button class="cover-button" data-action="series" data-series="${escapeHtml(series)}">
      <img class="cover" src="/thumb/${cover.id}" alt="${escapeHtml(series)} cover" loading="lazy">
      ${favorite ? '<span class="favorite-mark" title="Favorite">★</span>' : ""}
      ${missing ? `<span class="missing-mark">${missing} missing</span>` : ""}
      ${bilingual ? '<span class="bilingual-mark">Bilingual</span>' : ""}
    </button>
    <div class="card-copy"><button class="title-button plain" data-action="series" data-series="${escapeHtml(series)}">${escapeHtml(series)}</button>
      <div class="card-meta"><span>${volumes.length} ${volumes.length === 1 ? "edition" : "editions"}${bookmarks ? ` · ${bookmarks} saved` : ""}</span></div>
      ${resume ? `<button class="resume-link plain" data-action="reader" data-id="${resume.id}">Continue reading →</button>` : ""}
    </div>
  </article>`;
}

function filterGroups(groups) {
  return groups.filter(([, volumes]) => {
    if (state.libraryFilter === "bilingual") return isBilingual(volumes);
    if (state.libraryFilter === "japanese") return volumes.some(volume => volume.language === "jp" && volume.available);
    if (state.libraryFilter === "mokuro-ready") return volumes.some(volume => volume.language === "jp" && volume.mokuro_path);
    if (state.libraryFilter === "favorites") return volumes.some(volume => volume.favorite);
    if (state.libraryFilter === "bookmarked") return groupBookmarks(volumes) > 0;
    return true;
  });
}

function sortGroups(groups) {
  const newest = (volumes, field) => volumes.reduce((value, volume) => String(volume[field] || "") > value ? String(volume[field]) : value, "");
  return groups.sort(([seriesA, volumesA], [seriesB, volumesB]) => {
    if (state.librarySort === "title") return seriesA.localeCompare(seriesB, undefined, { sensitivity: "base" });
    if (state.librarySort === "added") return newest(volumesB, "created_at").localeCompare(newest(volumesA, "created_at"));
    return newest(volumesB, "last_read").localeCompare(newest(volumesA, "last_read")) || newest(volumesB, "created_at").localeCompare(newest(volumesA, "created_at"));
  });
}

function discoveryButton(value, label) {
  const active = state.libraryFilter === value;
  return `<button class="filter-chip ${active ? "active" : ""}" data-action="library-filter" data-filter="${value}" aria-pressed="${active}">${label}</button>`;
}

function stableReaderUrl() {
  return `${location.protocol}//${location.hostname}:${STABLE_PORT}/`;
}

async function reconnectLibrary() {
  try {
    await api("/api/health");
    return showLibrary();
  } catch (_) {
    try {
      await fetch(`${stableReaderUrl()}api/health`, { mode: "no-cors", cache: "no-store" });
      location.replace(stableReaderUrl());
    } catch (_) {
      toast("Reopen the Mokuro Reader app, then click Reconnect.", "warning");
    }
  }
}

function watchForRestart() {
  clearInterval(state.reconnectTimer);
  let attempts = 0;
  state.reconnectTimer = setInterval(async () => {
    attempts += 1;
    if (attempts > 90) return clearInterval(state.reconnectTimer);
    try {
      await fetch(`${stableReaderUrl()}api/health`, { mode: "no-cors", cache: "no-store" });
      clearInterval(state.reconnectTimer);
      location.replace(stableReaderUrl());
    } catch (_) { /* The local app is still restarting. */ }
  }, 2000);
}

async function showLibrary({ restore = false } = {}) {
  state.current = null;
  state.mapping = null;
  state.view = { kind: "library", series: "" };
  document.body.classList.remove("reading");
  try {
    state.library = await api("/api/library");
  } catch (error) {
    app.innerHTML = `<section class="empty error-card"><p class="eyebrow">LOCAL APP DISCONNECTED</p><h1>Reconnect to your library</h1><p>The app may have restarted while this tab was asleep. Your manga is still stored safely on this computer.</p><div class="button-row centered"><button data-action="reconnect">Reconnect</button><button class="secondary" data-action="reconnect-help">What do I do?</button></div><small>${escapeHtml(error.message)}</small></section>`;
    watchForRestart();
    return;
  }
  clearInterval(state.reconnectTimer);
  const query = $("#search").value.trim().toLocaleLowerCase();
  const visible = state.library.filter(volume =>
    `${volume.series} ${volume.title} ${volume.volume_label} ${volume.chapter_label}`.toLocaleLowerCase().includes(query)
  );
  const allGroups = groupLibrary(visible);
  const groups = sortGroups(filterGroups(allGroups));
  if (!state.library.length) {
    app.innerHTML = `<section class="empty"><div class="empty-icon">本</div><p class="eyebrow">BILINGUAL READING AND LOCAL OCR</p><h1>Welcome to your manga library</h1><p>Convert raw Japanese manga into selectable text for Yomitan, read any Mokuro manga, and match English translations with Japanese pages using ${BILINGUAL_MANGA_LINK}.</p><button data-action="add">+ Add Manga</button></section>`;
    return;
  }
  const recent = visible.filter(volume => volume.progress_page && volume.available).slice(0, 6);
  const totalGroups = groupLibrary(state.library);
  app.innerHTML = `
    <section class="welcome"><p class="eyebrow">WELCOME</p><p>Convert raw Japanese manga into selectable text for Yomitan, read any Mokuro manga, and use ${BILINGUAL_MANGA_LINK} to match English translations with Japanese pages automatically.</p></section>
    ${recent.length ? `<section class="shelf"><div class="section-heading"><div><p class="eyebrow">PICK UP WHERE YOU LEFT OFF</p><h1>Continue reading</h1></div></div><div class="continue-row">${recent.map(volume => `<button class="continue-card" data-action="reader" data-id="${volume.id}"><img src="/thumb/${volume.id}" alt="" loading="lazy"><span><b>${escapeHtml(volume.series)}</b><small>${escapeHtml(volumeName(volume))} · ${escapeHtml(contentTypeName(volume))}</small></span><i>→</i></button>`).join("")}</div></section>` : ""}
    <section class="shelf discovery"><div class="section-heading"><div><p class="eyebrow">DISCOVER YOUR LOCAL LIBRARY</p><h1>${totalGroups.length} series</h1></div></div>
      <div class="discovery-tools"><div class="filter-row">${discoveryButton("all", "All")}${discoveryButton("bilingual", "Bilingual")}${discoveryButton("japanese", "Japanese")}${discoveryButton("mokuro-ready", "Mokuro-Ready")}${discoveryButton("favorites", "Favorites")}${discoveryButton("bookmarked", "Bookmarked")}</div>
      <label class="sort-control">Sort <select id="library-sort"><option value="recent" ${state.librarySort === "recent" ? "selected" : ""}>Recent activity</option><option value="added" ${state.librarySort === "added" ? "selected" : ""}>Recently added</option><option value="title" ${state.librarySort === "title" ? "selected" : ""}>Title A–Z</option></select></label></div>
    </section>
    <section class="shelf"><div class="section-heading"><div><p class="eyebrow">${query ? "SEARCH RESULTS" : state.libraryFilter === "all" ? "THE LIBRARY" : state.libraryFilter.toUpperCase()}</p><h1>${groups.length} ${groups.length === 1 ? "series" : "series"}</h1></div><span class="muted">${visible.length} matching editions</span></div>
    ${groups.length ? `<div class="series-grid">${groups.map(([series, volumes]) => seriesCard(series, volumes)).join("")}</div>` : `<div class="no-results">No manga matches this search or filter.</div>`}</section>`;
  if (restore) restoreScroll(state.libraryScroll);
}

function matchingEditions(volume, volumes) {
  return volumes.filter(other => other.language !== volume.language && (
    (volume.volume_label && other.volume_label === volume.volume_label) ||
    (!volume.volume_label && !other.volume_label)
  ));
}

function volumeCard(volume, siblings) {
  const partner = matchingEditions(volume, siblings).length > 0;
  const warning = volume.error ? `<p class="volume-warning">${escapeHtml(volume.error)}</p>` : "";
  return `<article class="volume-card ${volume.available ? "" : "unavailable"}">
    <img src="/thumb/${volume.id}" alt="" loading="lazy">
    <div><div class="volume-top"><span class="content-badge ${contentTypeClass(volume)}">${escapeHtml(contentTypeName(volume))}</span>${partner ? '<span class="paired">Paired</span>' : '<span class="muted">Single edition</span>'}</div>
      <h3>${escapeHtml(volumeName(volume))}</h3><p>${volume.page_count} pages${volume.last_read ? ` · ${progressPercent(volume)}% read` : ""}${volume.bookmark_count ? ` · ${volume.bookmark_count} bookmarked` : ""}</p>${volume.last_read ? `<div class="volume-progress"><i style="width:${progressPercent(volume)}%"></i></div>` : ""}${warning}
      <div class="button-row">${volume.available ? `<button data-action="reader" data-id="${volume.id}">${volume.progress_page ? "Resume" : "Read"}</button>` : `<button data-action="relink" data-id="${volume.id}">Relink files</button>`}${volume.language === "jp" && !volume.mokuro_path ? `<button class="secondary" data-action="convert" data-id="${volume.id}">Convert with Mokuro</button>` : ""}<button class="secondary" data-action="align" data-id="${volume.id}">Review alignment</button><button class="secondary danger-outline" data-action="remove-volume" data-id="${volume.id}">Remove</button></div>
    </div>
  </article>`;
}

function showSeries(series, { restore = false } = {}) {
  document.body.classList.remove("reading");
  const volumes = state.library.filter(volume => volume.series === series);
  if (!volumes.length) return showLibrary();
  const favorite = volumes.some(volume => volume.favorite);
  const grouped = new Map();
  for (const volume of volumes) {
    const label = volume.volume_label || volume.chapter_label || volume.title;
    if (!grouped.has(label)) grouped.set(label, []);
    grouped.get(label).push(volume);
  }
  state.current = null;
  state.view = { kind: "series", series };
  app.innerHTML = `<section class="series-view"><button class="back plain" data-action="library">← Library</button>
    <div class="series-hero"><div><p class="eyebrow">${isBilingual(volumes) ? "BILINGUAL SERIES" : "MANGA SERIES"}</p><h1>${escapeHtml(series)}</h1><p>${volumes.length} imported ${volumes.length === 1 ? "edition" : "editions"}</p></div><div class="button-row"><button class="secondary" data-action="favorite" data-series="${escapeHtml(series)}">${favorite ? "★ Favorited" : "☆ Add favorite"}</button><button class="secondary danger-outline" data-action="remove-series" data-series="${escapeHtml(series)}">Remove series</button></div></div>
    ${[...grouped.entries()].sort(compareEditionGroups).map(([label, editions]) => `<section class="edition-group"><h2>${escapeHtml(label)}</h2><div class="volume-list">${editions.sort((a, b) => a.language.localeCompare(b.language)).map(volume => volumeCard(volume, volumes)).join("")}</div></section>`).join("")}
  </section>`;
  if (restore) restoreScroll(state.seriesScroll.get(series));
}

async function openReader(volumeId, requestedPageId = null, displayedIds = []) {
  try {
    state.current = await api(`/api/volume/${volumeId}`);
    if (!state.current.available) {
      toast(state.current.error, "error");
      return showSeries(state.current.series);
    }
    const target = requestedPageId || state.current.progress_page;
    const found = state.current.pages.findIndex(page => page.id === target);
    state.pageIndex = found >= 0 ? found : 0;
    state.displayedIds = displayedIds.length ? displayedIds : [];
    state.view = { kind: "reader", series: state.current.series };
    document.body.classList.add("reading");
    renderReader();
  } catch (error) {
    toast(error.message, "error");
  }
}

function currentDisplayPages() {
  if (!state.current) return [];
  const ids = state.displayedIds.length ? state.displayedIds : [state.current.pages[state.pageIndex]?.id];
  return ids.map(id => state.current.pages.find(page => page.id === id)).filter(Boolean);
}

function friendlyChapter(value) {
  const label = String(value || "").trim();
  if (!label || /^[0-9a-f-]{24,}$/i.test(label) || /^(?:bafy|Qm)[a-z0-9]{20,}$/i.test(label)) return "";
  return label;
}

function chapterChoices() {
  const labels = [];
  state.current.pages.forEach(page => {
    const label = friendlyChapter(page.chapter_label || state.current.chapter_label);
    if (!label) return;
    if (!labels.some(item => item.label === label)) labels.push({ label, ordinal: page.ordinal });
  });
  return labels;
}

function chapterOptions(choices) {
  const current = friendlyChapter(state.current.pages[state.pageIndex]?.chapter_label || state.current.chapter_label);
  return choices.map(item => `<option value="${item.ordinal}" ${current === item.label ? "selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
}

function bookmarkOptions() {
  const ids = new Set(state.current?.bookmark_page_ids || []);
  return state.current.pages
    .filter(page => ids.has(page.id))
    .map(page => {
      const section = friendlyChapter(page.chapter_label || state.current.chapter_label) || volumeName(state.current);
      return `<option value="${page.id}">${escapeHtml(section)} · Page ${page.ordinal + 1}</option>`;
    })
    .join("");
}

function renderReader() {
  const pages = currentDisplayPages();
  if (!pages.length) return;
  const first = pages[0];
  const paired = matchingEditions(state.current, state.library).length > 0;
  const bookmarked = (state.current.bookmark_page_ids || []).includes(first.id);
  const previousKey = state.direction === "rtl" ? "→" : "←";
  const nextKey = state.direction === "rtl" ? "←" : "→";
  const hasOcr = state.current.language === "jp" && Boolean(state.current.mokuro_path);
  const chapters = chapterChoices();
  const previousButton = `<button class="secondary reader-move" data-action="move" data-direction="-1" ${state.pageIndex <= 0 ? "disabled" : ""}>${previousKey} Previous</button>`;
  const nextButton = `<button class="reader-move" data-action="move" data-direction="1" ${state.pageIndex + pages.length >= state.current.pages.length ? "disabled" : ""}>Next ${nextKey}</button>`;
  const leftButton = state.direction === "rtl" ? nextButton : previousButton;
  const rightButton = state.direction === "rtl" ? previousButton : nextButton;
  app.innerHTML = `<section class="reader fit-${state.fit} direction-${state.direction} ocr-${state.ocrDisplay} ${state.clickNavigation ? "click-nav" : ""}" style="--zoom:${state.zoom}">
    <nav class="reader-tools">
      <button class="secondary reader-back" data-action="series" data-series="${escapeHtml(state.current.series)}">← Library</button>
      <div class="reader-position"><b>${escapeHtml(state.current.series)}</b><span>${escapeHtml(volumeName(state.current))} · ${state.pageIndex + 1}/${state.current.pages.length}</span></div>
      <button class="language-toggle" data-action="language" ${paired ? "" : "disabled"}>${state.current.language.toUpperCase()} <span>⇄</span> ${state.current.language === "jp" ? "EN" : "JP"} <kbd>L</kbd></button>
      ${chapters.length > 1 ? `<select class="chapter-select" aria-label="Chapter" data-action="chapter">${chapterOptions(chapters)}</select>` : ""}
      <button class="secondary" data-action="bookmark">${bookmarked ? "★ Saved" : "☆ Bookmark"} <kbd>B</kbd></button>
      <select class="bookmark-select" aria-label="Go to bookmark" data-action="bookmark-jump" ${(state.current.bookmark_page_ids || []).length ? "" : "disabled"}><option value="">Bookmarks (${(state.current.bookmark_page_ids || []).length})</option>${bookmarkOptions()}</select>
      <button class="secondary" data-action="direction" title="Switch reading direction">${state.direction.toUpperCase()}</button>
      <select class="ocr-select" aria-label="OCR display" data-action="ocr-display" ${hasOcr ? "" : "disabled"}><option value="hover" ${state.ocrDisplay === "hover" ? "selected" : ""}>OCR: Hover</option><option value="visible" ${state.ocrDisplay === "visible" ? "selected" : ""}>OCR: Show</option><option value="hidden" ${state.ocrDisplay === "hidden" ? "selected" : ""}>OCR: Hide</option></select>
      <button class="secondary" data-action="click-navigation" title="Toggle click-to-turn">Click: ${state.clickNavigation ? "On" : "Off"}</button>
      <button class="icon secondary" data-action="fit" title="Fit screen / width">${state.fit === "screen" ? "↔" : "↕"}</button>
      <button class="icon secondary" data-action="zoom-out" title="Zoom out">−</button><span class="zoom-label">${Math.round(state.zoom * 100)}%</span><button class="icon secondary" data-action="zoom-in" title="Zoom in">+</button>
      <button class="icon secondary" data-action="fullscreen" title="Fullscreen">⛶</button>
      <button class="icon secondary" data-action="align" data-id="${state.current.id}" title="Review page alignment">A</button>
    </nav>
    <div class="page-stage" aria-label="Manga pages">${pages.map(page => `<div class="page-wrap" data-page-id="${page.id}"><img class="page-image" src="/media/${page.id}" alt="Page ${page.ordinal + 1}" draggable="false"><div class="ocr-layer" aria-label="Selectable Japanese text"></div></div>`).join("")}</div>
    <nav class="reader-nav" aria-label="Page navigation">${leftButton}<span>${state.direction === "rtl" ? "Right-to-left" : "Left-to-right"} · ${state.pageIndex + 1}/${state.current.pages.length}</span>${rightButton}</nav>
    <div class="reading-progress"><i style="width:${(state.pageIndex + 1) / state.current.pages.length * 100}%"></i></div>
  </section>`;
  jsonPost("/api/progress", { volume_id: state.current.id, page_id: first.id }).catch(() => {});
  for (const page of pages) if (state.current.language === "jp") renderMokuro(page);
  preloadNearby();
}

async function renderMokuro(page) {
  const wrapper = $(`.page-wrap[data-page-id="${page.id}"]`);
  if (!wrapper || !state.current.mokuro_path) return;
  const image = $(".page-image", wrapper);
  const layer = $(".ocr-layer", wrapper);
  const data = await api(`/api/mokuro/${page.id}`).catch(() => ({ blocks: [] }));
  if (!data.blocks?.length) return;
  const draw = () => {
    layer.replaceChildren();
    const scaleX = image.clientWidth / (Number(data.img_width) || image.naturalWidth || 1);
    const scaleY = image.clientHeight / (Number(data.img_height) || image.naturalHeight || 1);
    for (const [index, block] of data.blocks.entries()) {
      const blockBox = Array.isArray(block.box) ? block.box : [block.box?.x, block.box?.y, block.box?.x2 ?? (block.box?.x || 0) + (block.box?.w || 0), block.box?.y2 ?? (block.box?.y || 0) + (block.box?.h || 0)];
      const [bx1, by1, bx2, by2] = blockBox.map(value => Number(value || 0));
      const lines = (block.lines || []).map(line => typeof line === "string" ? line : (line?.text || line?.content || "")).filter(Boolean);
      const vertical = Boolean(block.vertical || block.writing_mode === "vertical-rl");
      const coordinates = Array.isArray(block.lines_coords) ? block.lines_coords : [];

      for (const [lineIndex, line] of lines.entries()) {
        const quad = Array.isArray(coordinates[lineIndex]) ? coordinates[lineIndex] : [];
        const points = quad.map(point => Array.isArray(point) ? point.map(Number) : []).filter(point => point.length >= 2 && point.every(Number.isFinite));
        let x1, y1, x2, y2;
        if (points.length >= 2) {
          x1 = Math.min(...points.map(point => point[0]));
          y1 = Math.min(...points.map(point => point[1]));
          x2 = Math.max(...points.map(point => point[0]));
          y2 = Math.max(...points.map(point => point[1]));
        } else if (vertical) {
          const column = Math.max(1, (bx2 - bx1) / Math.max(1, lines.length));
          x1 = bx2 - column * (lineIndex + 1);
          x2 = bx2 - column * lineIndex;
          y1 = by1;
          y2 = by2;
        } else {
          const row = Math.max(1, (by2 - by1) / Math.max(1, lines.length));
          x1 = bx1;
          x2 = bx2;
          y1 = by1 + row * lineIndex;
          y2 = by1 + row * (lineIndex + 1);
        }

        const sourceWidth = Math.max(1, x2 - x1);
        const sourceHeight = Math.max(1, y2 - y1);
        const mainExtent = vertical ? sourceHeight : sourceWidth;
        const crossExtent = vertical ? sourceWidth : sourceHeight;
        const characters = Math.max(1, Array.from(line).length);
        const requestedSize = Math.max(1, Number(block.font_size || 16));
        const fittedSize = Math.min(requestedSize, crossExtent * 1.05, mainExtent / (characters * 0.92));
        const text = document.createElement("span");
        text.className = "ocr-line";
        text.lang = "ja";
        text.dataset.blockIndex = String(index);
        text.dataset.lineIndex = String(lineIndex);
        text.textContent = line;
        Object.assign(text.style, {
          left: `${x1 * scaleX}px`, top: `${y1 * scaleY}px`,
          width: `${sourceWidth * scaleX}px`, height: `${sourceHeight * scaleY}px`,
          fontSize: `${Math.max(5, fittedSize * Math.min(scaleX, scaleY))}px`,
          writingMode: vertical ? "vertical-rl" : "horizontal-tb",
          zIndex: String(100 + index),
        });
        layer.append(text);
      }
    }
  };
  if (!image.complete) await new Promise(resolve => image.addEventListener("load", resolve, { once: true }));
  draw();
  new ResizeObserver(draw).observe(image);
}

function movePage(direction) {
  if (!state.current) return;
  const step = direction > 0 ? Math.max(1, currentDisplayPages().length) : 1;
  state.pageIndex = Math.max(0, Math.min(state.current.pages.length - 1, state.pageIndex + direction * step));
  state.displayedIds = [];
  renderReader();
}

async function switchLanguage() {
  const page = currentDisplayPages()[0];
  if (!page) return;
  try {
    const match = await api(`/api/corresponding/${page.id}?side=${state.current.language}`);
    if (!match.page_ids?.length) {
      toast(match.review ? "This correspondence is uncertain. Press A to review it." : "No confirmed corresponding page yet. Press A to align editions.", "warning");
      return;
    }
    await openReader(match.volume_id, match.page_ids[0], match.page_ids);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function toggleBookmark() {
  const page = currentDisplayPages()[0];
  if (!page || !state.current) return;
  const ids = new Set(state.current.bookmark_page_ids || []);
  const bookmarked = !ids.has(page.id);
  const result = await jsonPost("/api/bookmark", {
    volume_id: state.current.id,
    page_id: page.id,
    bookmarked,
  });
  if (bookmarked) ids.add(page.id); else ids.delete(page.id);
  state.current.bookmark_page_ids = [...ids];
  const libraryVolume = state.library.find(volume => volume.id === state.current.id);
  if (libraryVolume) libraryVolume.bookmark_count = result.bookmark_count;
  renderReader();
  toast(bookmarked ? `Bookmarked page ${page.ordinal + 1}` : `Removed bookmark from page ${page.ordinal + 1}`);
}

function handlePageClick(event, stage) {
  if (!state.current || !state.clickNavigation || event.target.closest(".ocr-line")) return;
  if (window.getSelection?.().toString()) return;
  const bounds = stage.getBoundingClientRect();
  const leftHalf = event.clientX < bounds.left + bounds.width / 2;
  const forward = state.direction === "rtl" ? leftHalf : !leftHalf;
  movePage(forward ? 1 : -1);
}

async function preloadNearby() {
  if (!state.current) return;
  const candidates = [state.current.pages[state.pageIndex - 1], state.current.pages[state.pageIndex + currentDisplayPages().length]].filter(Boolean);
  for (const page of candidates) (new Image()).src = `/media/${page.id}`;
  const current = currentDisplayPages()[0];
  if (current) {
    const match = await api(`/api/corresponding/${current.id}?side=${state.current.language}`).catch(() => null);
    for (const id of match?.page_ids || []) (new Image()).src = `/media/${id}`;
  }
}

function adjustZoom(delta) {
  state.zoom = delta === 0 ? 1 : Math.max(0.5, Math.min(3, state.zoom + delta));
  jsonPost("/api/settings", { reader_zoom: state.zoom }).catch(() => {});
  renderReader();
}

async function toggleFavorite(series) {
  const favorite = !state.library.some(volume => volume.series === series && volume.favorite);
  await jsonPost("/api/favorite", { series, favorite });
  state.library.filter(volume => volume.series === series).forEach(volume => { volume.favorite = favorite ? 1 : 0; });
  showSeries(series);
  toast(favorite ? "Added to favorites" : "Removed from favorites");
}

function openRemoval({ volumeId = null, series = null }) {
  const volumes = volumeId
    ? state.library.filter(item => item.id === volumeId)
    : state.library.filter(item => item.series === series);
  if (!volumes.length) return;
  const label = volumeId ? volumeName(volumes[0]) : series;
  const originalCount = volumes.filter(volume => volume.original_available).length;
  state.removal = { volumeId, series, label, originalCount };
  $("#remove-heading").textContent = volumeId ? `Remove ${label}` : `Remove ${series}`;
  $("#remove-copy").textContent = volumeId
    ? "The edition, its app-managed copy, reading progress, bookmarks, and alignments will be removed from the library."
    : `All ${volumes.length} imported editions, their app-managed copies, reading progress, bookmarks, and alignments will be removed.`;
  $("#remove-originals").checked = false;
  $("#remove-originals").disabled = !originalCount;
  $("#remove-originals-note").textContent = originalCount
    ? `${originalCount} original ${originalCount === 1 ? "source is" : "sources are"} available outside the app. Checked sources will be moved to Trash or the Recycle Bin.`
    : "No separate original source is available; only app-managed data will be removed.";
  $("#remove-error").textContent = "";
  $("#remove-dialog").showModal();
}

async function confirmRemoval() {
  if (!state.removal) return;
  const button = $("#remove-confirm");
  button.disabled = true;
  $("#remove-error").textContent = "";
  const removal = state.removal;
  try {
    const result = await jsonPost("/api/library/remove", {
      volume_id: removal.volumeId,
      series: removal.series,
      delete_originals: $("#remove-originals").checked,
    });
    $("#remove-dialog").close();
    await showLibrary();
    if (removal.volumeId && state.library.some(item => item.series === removal.series)) showSeries(removal.series);
    const originals = result.deletion_warning
      ? ` ${result.deletion_warning}`
      : result.originals_trashed
      ? ` ${result.originals_trashed} original ${result.originals_trashed === 1 ? "source was" : "sources were"} moved to Trash.`
      : " Original files were preserved.";
    toast(`Removed ${result.removed} ${result.removed === 1 ? "edition" : "editions"}.${originals}`);
  } catch (error) {
    $("#remove-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function removeVolume(volumeId) {
  const volume = state.library.find(item => item.id === volumeId);
  if (!volume) return;
  openRemoval({ volumeId, series: volume.series });
}

async function removeSeries(series) {
  openRemoval({ series });
}

async function monitorConversion(job) {
  state.conversionJob = job;
  const dialog = $("#conversion-dialog");
  if (!dialog.open) dialog.showModal();
  $("#conversion-error").textContent = "";
  $("#conversion-done").hidden = true;
  while (state.conversionJob?.id === job.id && !["complete", "failed"].includes(job.status)) {
    $("#conversion-label").textContent = job.label || "Japanese manga";
    $("#conversion-progress span").textContent = job.message || "Working locally…";
    const percent = ({ queued: 3, installing: 12, working: 25, preparing: 32, rendering: 42, converting: 62 })[job.status] || 8;
    $("#conversion-progress div").style.width = `${percent}%`;
    await new Promise(resolve => setTimeout(resolve, 900));
    try {
      job = await api(`/api/conversion/status?job_id=${encodeURIComponent(job.id)}`);
      state.conversionJob = job;
    } catch (error) {
      $("#conversion-error").textContent = `${error.message}. The local job may still be running; reopen the app and library to check.`;
      return;
    }
  }
  if (job.status === "complete") {
    $("#conversion-progress div").style.width = "100%";
    $("#conversion-progress span").textContent = job.message;
    $("#conversion-done").hidden = !job.volume_id;
    $("#conversion-done").dataset.id = job.volume_id || "";
    await showLibrary();
    queuePairingReview(job.pairing_review);
    toast(job.message);
    showNextPairing();
  } else {
    $("#conversion-progress div").style.width = "100%";
    $("#conversion-progress span").textContent = "Conversion stopped";
    $("#conversion-error").textContent = job.error || "The converter could not finish.";
  }
}

function openConversionOptions(volumeId) {
  const volume = state.library.find(item => item.id === volumeId);
  if (!volume) return;
  state.conversionVolumeId = volumeId;
  $("#conversion-delete-original").checked = false;
  $("#conversion-delete-original").disabled = !volume.original_available;
  $("#conversion-delete-note").textContent = volume.original_available
    ? "Optional. The source is moved only after every page and the OCR output are verified."
    : "No separate original source is available; the app-managed copy will be kept.";
  $("#conversion-options-error").textContent = "";
  $("#conversion-options-dialog").showModal();
}

async function convertVolume(volumeId, deleteOriginal = false) {
  const job = await jsonPost("/api/conversion/start", { volume_id: volumeId, delete_original: deleteOriginal });
  $("#conversion-options-dialog").close();
  monitorConversion(job).catch(error => toast(error.message, "error"));
}

function queuePairingReview(review) {
  if (!review?.volume_id || !review.candidates?.length) return;
  if (!state.pendingPairings.some(item => item.volume_id === review.volume_id)) state.pendingPairings.push(review);
}

function showNextPairing() {
  if ($("#pairing-dialog").open || !state.pendingPairings.length) return;
  state.pairingReview = state.pendingPairings.shift();
  const review = state.pairingReview;
  $("#pairing-copy").textContent = `“${review.title}” may match an edition already in your library. Choose the correct opposite-language edition, or keep it separate.`;
  $("#pairing-list").innerHTML = review.candidates.map(candidate => `
    <button type="button" class="pairing-candidate" data-action="confirm-pair" data-peer-id="${candidate.id}">
      <span><b>${escapeHtml(candidate.series)}</b><small>${escapeHtml([candidate.volume_label || candidate.chapter_label, candidate.language === "jp" ? "Japanese" : "English", `${candidate.page_count} pages`].filter(Boolean).join(" · "))}</small></span>
      <strong>${Math.round(candidate.score * 100)}% match</strong>
    </button>`).join("");
  $("#pairing-error").textContent = "";
  $("#pairing-dialog").showModal();
}

async function confirmPair(peerId) {
  if (!state.pairingReview) return;
  $("#pairing-error").textContent = "";
  try {
    const result = await jsonPost("/api/pairing/confirm", {
      volume_id: state.pairingReview.volume_id,
      peer_id: peerId,
    });
    $("#pairing-dialog").close();
    state.pairingReview = null;
    await showLibrary();
    toast(`Paired and aligned ${result.pages_mapped} page correspondences.`);
    showNextPairing();
  } catch (error) {
    $("#pairing-error").textContent = error.message;
  }
}

async function openAlignment(volumeId) {
  const origin = state.library.find(volume => volume.id === volumeId) || state.current;
  if (!origin) return;
  const jpMeta = origin.language === "jp" ? origin : matchingEditions(origin, state.library).find(volume => volume.language === "jp");
  const enMeta = origin.language === "en" ? origin : matchingEditions(origin, state.library).find(volume => volume.language === "en");
  if (!jpMeta || !enMeta) {
    toast("Import the other language before reviewing alignment.", "warning");
    return;
  }
  const [jp, en] = await Promise.all([api(`/api/volume/${jpMeta.id}`), api(`/api/volume/${enMeta.id}`)]);
  state.mapping = {
    jp, en,
    ji: state.current?.id === jp.id ? state.pageIndex : 0,
    ei: state.current?.id === en.id ? state.pageIndex : 0,
    jpCount: 1, enCount: 1,
    returnId: origin.id,
  };
  document.body.classList.add("reading");
  renderAlignment();
}

function alignmentPages(side) {
  const mapping = state.mapping;
  const volume = mapping[side];
  const index = mapping[side === "jp" ? "ji" : "ei"];
  const count = mapping[`${side}Count`];
  return volume.pages.slice(index, index + count);
}

function renderAlignment() {
  const mapping = state.mapping;
  const jpPages = alignmentPages("jp");
  const enPages = alignmentPages("en");
  app.innerHTML = `<section class="alignment"><nav class="reader-tools"><button class="secondary" data-action="alignment-cancel">Cancel</button><div class="reader-position"><b>Confirm correspondence</b><span>Manual choices are saved permanently</span></div><button data-action="map-confirm">These pages match</button></nav>
    <div class="alignment-grid">
      <section><header><div><span class="lang yes">JP</span><b>${mapping.ji + 1}/${mapping.jp.pages.length}</b></div><div><button class="icon secondary" data-action="map-step" data-side="jp" data-direction="-1">→</button><button class="icon secondary" data-action="map-step" data-side="jp" data-direction="1">←</button></div></header><div class="alignment-images">${jpPages.map(page => `<img src="/media/${page.id}" alt="Japanese page ${page.ordinal + 1}">`).join("")}</div><footer><label><input type="checkbox" data-action="map-spread" data-side="jp" ${mapping.jpCount === 2 ? "checked" : ""} ${mapping.ji + 1 >= mapping.jp.pages.length ? "disabled" : ""}> Include next page (spread)</label><button class="plain" data-action="map-unmatched" data-side="jp">Mark JP unmatched</button></footer></section>
      <section><header><div><span class="lang yes">EN</span><b>${mapping.ei + 1}/${mapping.en.pages.length}</b></div><div><button class="icon secondary" data-action="map-step" data-side="en" data-direction="-1">→</button><button class="icon secondary" data-action="map-step" data-side="en" data-direction="1">←</button></div></header><div class="alignment-images">${enPages.map(page => `<img src="/media/${page.id}" alt="English page ${page.ordinal + 1}">`).join("")}</div><footer><label><input type="checkbox" data-action="map-spread" data-side="en" ${mapping.enCount === 2 ? "checked" : ""} ${mapping.ei + 1 >= mapping.en.pages.length ? "disabled" : ""}> Include next page (spread)</label><button class="plain" data-action="map-unmatched" data-side="en">Mark EN unmatched</button></footer></section>
    </div></section>`;
}

async function saveMapping(jpPages, enPages) {
  const result = await jsonPost("/api/map", {
    series: state.mapping.jp.series,
    jp_pages: jpPages.map(page => page.id),
    en_pages: enPages.map(page => page.id),
  });
  toast(result.inferred_neighbors ? `Mapping saved; ${result.inferred_neighbors} safe neighbor mapping inferred.` : "Mapping saved.");
  await openReader(state.mapping.returnId);
}

function bulkImportable(item) {
  return ["ready", "review", "import-error"].includes(item.status);
}

function bulkStatus(item) {
  return ({
    ready: "Ready",
    review: "Needs review",
    error: "Problem",
    existing: "Already added",
    importing: "Importing…",
    imported: "Added",
    "import-error": "Import failed",
  })[item.status] || item.status;
}

function bulkKind(item) {
  if (item.content_type === "bilingual" || item.recognized) return `Bilingual · ${item.language === "en" ? "English" : "Japanese"}`;
  if (item.content_type === "mokuro" || item.kind === "mokuro" || item.has_mokuro) return "Mokuro · Japanese";
  return item.language === "en" ? "Raw · English" : "Raw · Japanese";
}

function editionChoice(item) {
  if (item.content_type === "bilingual") return `bilingual-${item.language || "jp"}`;
  if (item.content_type === "mokuro" || item.has_mokuro) return "mokuro";
  return `raw-${item.language || "jp"}`;
}

function editionValues(choice) {
  if (choice === "mokuro") return { language: "jp", content_type: "mokuro" };
  const [contentType, language] = String(choice).split("-", 2);
  return {
    language: ["jp", "en"].includes(language) ? language : "jp",
    content_type: ["bilingual", "raw"].includes(contentType) ? contentType : "raw",
  };
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return "";
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(value / 1024))} KB`;
}

function selectedBulkItems() {
  return (state.bulk.job?.items || []).filter(item => item.selected && bulkImportable(item));
}

function updateBulkSummary() {
  const items = state.bulk.job?.items || [];
  const counts = {
    ready: items.filter(item => item.status === "ready").length,
    review: items.filter(item => item.status === "review").length,
    problems: items.filter(item => ["error", "import-error"].includes(item.status)).length,
    existing: items.filter(item => item.status === "existing").length,
    imported: items.filter(item => item.status === "imported").length,
  };
  const selected = selectedBulkItems();
  const missingLanguage = selected.some(item => !["jp", "en"].includes(item.language));
  $("#bulk-summary").innerHTML = `
    <div><b>${items.length}</b><span>found</span></div>
    <div class="good"><b>${counts.ready}</b><span>ready</span></div>
    <div class="review"><b>${counts.review}</b><span>review</span></div>
    <div class="bad"><b>${counts.problems}</b><span>problems</span></div>
    <div><b>${counts.existing}</b><span>already added</span></div>
    ${counts.imported ? `<div class="good"><b>${counts.imported}</b><span>added now</span></div>` : ""}`;
  const button = $("#bulk-import");
  button.textContent = selected.length ? `Import ${selected.length} selected` : "Import selected";
  button.disabled = state.bulk.importing || !selected.length || missingLanguage;
}

function renderBulkItem(item) {
  const disabled = !bulkImportable(item) || state.bulk.importing;
  const languageLocked = disabled || item.recognized || item.kind === "mokuro";
  const pages = item.image_count ? `${item.image_count}${item.image_count_exact === false ? "+" : ""} pages` : "";
  const details = [bulkKind(item), pages, formatBytes(item.size)].filter(Boolean).join(" · ");
  const message = item.error || item.note || "";
  return `<article class="bulk-item bulk-${item.status}" data-bulk-id="${item.id}" role="listitem">
    <label class="bulk-choice"><input type="checkbox" data-bulk-select ${item.selected ? "checked" : ""} ${disabled ? "disabled" : ""}><span class="bulk-status">${escapeHtml(bulkStatus(item))}</span></label>
    <div class="bulk-details">
      <div class="bulk-item-head"><div><strong title="${escapeHtml(item.path)}">${escapeHtml(item.series || item.title || item.name)}</strong><span>${escapeHtml([item.volume_label || item.chapter_label, item.name, details].filter(Boolean).join(" · "))}</span></div><span class="recognized-mark type-${escapeHtml(item.content_type || "raw")}">${escapeHtml(bulkKind(item))}</span></div>
      <details class="bulk-advanced"><summary>Details &amp; corrections</summary><div class="bulk-fields">
        <label>Edition type<select data-bulk-field="edition_type" ${languageLocked ? "disabled" : ""}><option value="bilingual-jp" ${editionChoice(item) === "bilingual-jp" ? "selected" : ""}>Bilingual · Japanese</option><option value="bilingual-en" ${editionChoice(item) === "bilingual-en" ? "selected" : ""}>Bilingual · English</option><option value="mokuro" ${editionChoice(item) === "mokuro" ? "selected" : ""}>Mokuro-ready Japanese</option><option value="raw-jp" ${editionChoice(item) === "raw-jp" ? "selected" : ""}>Raw Japanese</option><option value="raw-en" ${editionChoice(item) === "raw-en" ? "selected" : ""}>Raw English</option></select></label>
        <label>Series<input data-bulk-field="series" value="${escapeHtml(item.series)}" ${disabled ? "disabled" : ""}></label>
        <label>Title<input data-bulk-field="title" value="${escapeHtml(item.title)}" ${disabled ? "disabled" : ""}></label>
        <label>Volume<input data-bulk-field="volume_label" value="${escapeHtml(item.volume_label)}" ${disabled ? "disabled" : ""}></label>
        <label>Chapter<input data-bulk-field="chapter_label" value="${escapeHtml(item.chapter_label)}" ${disabled ? "disabled" : ""}></label>
      </div></details>
      ${message ? `<p class="bulk-note">${escapeHtml(message)}</p>` : ""}
    </div>
  </article>`;
}

function renderBulkResults() {
  const items = state.bulk.job?.items || [];
  const query = state.bulk.query.trim().toLocaleLowerCase();
  const visible = items.filter(item => {
    const statusMatches = state.bulk.filter === "all" ||
      (state.bulk.filter === "error" ? ["error", "import-error"].includes(item.status) : item.status === state.bulk.filter);
    const queryMatches = !query || `${item.name} ${item.series} ${item.title} ${item.volume_label}`.toLocaleLowerCase().includes(query);
    return statusMatches && queryMatches;
  });
  $("#bulk-dialog").classList.add("has-results");
  $("#bulk-results").hidden = false;
  $("#bulk-root").textContent = state.bulk.job?.root || "";
  $("#bulk-list").innerHTML = visible.length
    ? visible.map(renderBulkItem).join("")
    : '<div class="no-results">No scan results match this filter.</div>';
  updateBulkSummary();
}

function updateBulkScanProgress(job) {
  const progress = $("#bulk-scan-progress");
  const percentage = job.total ? Math.max(3, Math.min(100, job.processed / job.total * 100)) : 3;
  $("div", progress).style.width = `${percentage}%`;
  $("span", progress).textContent = job.total
    ? `Checking ${job.processed} of ${job.total}: ${job.current}`
    : job.current;
  $("#bulk-root").textContent = job.root || "";
}

async function beginBulkScan() {
  state.bulk = { scanId: null, job: null, filter: "all", query: "", importing: false };
  $("#bulk-dialog").classList.remove("has-results");
  $("#bulk-results").hidden = true;
  $("#bulk-error").textContent = "";
  $("#bulk-import-progress").hidden = true;
  $("#bulk-import").disabled = true;
  $("#bulk-filter").value = "all";
  $("#bulk-search").value = "";
  $("#bulk-scan-progress div").style.width = "3%";
  $("#bulk-scan-progress span").textContent = "Waiting for folder selection…";
  $("#bulk-root").textContent = "";
  $("#import-dialog").close();
  $("#bulk-dialog").showModal();
  try {
    const chosen = await jsonPost("/api/bulk/pick", {});
    if (chosen.cancelled) {
      $("#bulk-scan-progress span").textContent = "No folder selected.";
      return;
    }
    const started = await jsonPost("/api/bulk/scan/start", { path: chosen.path });
    state.bulk.scanId = started.id;
    state.bulk.job = started;
    updateBulkScanProgress(started);
    while (state.bulk.scanId === started.id && state.bulk.job.status === "scanning") {
      await new Promise(resolve => setTimeout(resolve, 400));
      state.bulk.job = await api(`/api/bulk/status?scan_id=${encodeURIComponent(started.id)}`);
      updateBulkScanProgress(state.bulk.job);
    }
    if (state.bulk.job.status === "failed") throw new Error(state.bulk.job.error || "Folder scan failed");
    renderBulkResults();
  } catch (error) {
    $("#bulk-error").textContent = error.message;
    $("#bulk-scan-progress span").textContent = "Scan stopped";
  }
}

async function importBulkSelection() {
  const selected = selectedBulkItems();
  if (!selected.length || state.bulk.importing) return;
  if (selected.some(item => !["jp", "en"].includes(item.language))) {
    $("#bulk-error").textContent = "Choose Japanese or English for every selected item.";
    return;
  }
  state.bulk.importing = true;
  $("#bulk-error").textContent = "";
  const progress = $("#bulk-import-progress");
  progress.hidden = false;
  renderBulkResults();
  let imported = 0;
  let failed = 0;
  for (const [index, item] of selected.entries()) {
    $("div", progress).style.width = `${Math.max(2, index / selected.length * 100)}%`;
    $("span", progress).textContent = `Importing ${index + 1} of ${selected.length}: ${item.name}`;
    item.status = "importing";
    try {
      const result = await jsonPost("/api/bulk/import-one", {
        scan_id: state.bulk.scanId,
        item_id: item.id,
        metadata: {
          language: item.language,
          content_type: item.content_type,
          series: item.series,
          title: item.title,
          volume_label: item.volume_label,
          chapter_label: item.chapter_label,
        },
        friendly_names: $("#bulk-friendly-names").checked,
        delete_original: $("#bulk-delete-originals").checked && item.kind === "archive",
      });
      item.status = "imported";
      item.selected = false;
      item.error = "";
      item.note = `Imported ${result.pages} pages.`;
      queuePairingReview(result.pairing_review);
      imported += 1;
    } catch (error) {
      item.status = "import-error";
      item.selected = false;
      item.error = error.message;
      failed += 1;
    }
    $("div", progress).style.width = `${(index + 1) / selected.length * 100}%`;
  }
  state.bulk.importing = false;
  await showLibrary();
  $("span", progress).textContent = `${imported} added${failed ? ` · ${failed} failed` : ""}`;
  renderBulkResults();
  toast(
    failed ? `${imported} manga added; ${failed} need attention. The rest were not interrupted.` : `${imported} manga added to your library.`,
    failed ? "warning" : "info",
  );
  showNextPairing();
}

function resetImportDialog(relinkVolumeId = null) {
  state.selectedFiles = [];
  state.selectedSource = null;
  state.relinkVolumeId = relinkVolumeId;
  $("#folder-input").value = "";
  $("#archive-input").value = "";
  $("#import-error").textContent = "";
  $("#import-progress").hidden = true;
  $("#import-submit").disabled = true;
  $("#dropzone strong").textContent = "Drop one Mokuro folder, raw image folder, PDF, CBZ, ZIP, or TAR";
  const relink = state.library.find(volume => volume.id === relinkVolumeId);
  $("#import-heading").textContent = relink ? `Relink ${volumeName(relink)}` : "Add manga";
  if (relink) $("#import-copy").textContent = `Choose the moved ${relink.page_count}-page edition. Existing progress, bookmarks, and mappings will be preserved.`;
  else $("#import-copy").innerHTML = `Any Mokuro manga works. Archives from ${BILINGUAL_MANGA_LINK} are recognized, named, grouped, and page-aligned automatically. The app caches about 14 MB of recognition metadata from that linked archive on first use; manga pages never leave this computer.`;
  $("#metadata-fields").hidden = Boolean(relink);
  $("#metadata-fields").open = false;
  $("#auto-detection").hidden = Boolean(relink);
  $("#convert-choice").hidden = Boolean(relink);
  $("#bulk-entry").hidden = Boolean(relink);
  $("#single-divider").hidden = Boolean(relink);
  $("#choose-manga").textContent = relink ? "Choose the moved manga…" : "Choose one manga…";
  $("#import-type").value = "auto";
  $("#convert-after-import").checked = false;
  $("#convert-after-import").disabled = false;
  $("#friendly-names").checked = true;
  $("#delete-original-after-import").checked = false;
  $("#delete-original-after-import").disabled = true;
  $("#delete-original-note").textContent = "Choose one manga with the button above to make this available.";
  $("#detected-type").textContent = "Choose manga to identify it";
  $("#detected-copy").textContent = "The reader finds its type and title information for you.";
  if (!relink) for (const id of ["import-series", "import-title", "import-volume", "import-chapter"]) $(`#${id}`).value = "";
  $("#import-dialog").showModal();
  api("/api/status").then(status => {
    $("#import-library-path").textContent = status.library_dir;
  }).catch(() => { $("#import-library-path").textContent = "Private Application Support library"; });
}

function selectedEditionValues() {
  const choice = $("#import-type").value;
  if (choice === "auto") return { language: "auto", content_type: "auto" };
  return editionValues(choice);
}

function updateDeleteOriginalAvailability() {
  const checkbox = $("#delete-original-after-import");
  if (state.relinkVolumeId || !state.selectedSource) {
    checkbox.checked = false;
    checkbox.disabled = true;
    $("#delete-original-note").textContent = state.selectedFiles.length
      ? "Chrome supplied a private copy, so the original location is not accessible."
      : "Choose one manga with the button above to make this available.";
    return;
  }
  const source = state.selectedSource;
  checkbox.disabled = false;
  $("#delete-original-note").textContent = source.kind === "folder" && !$("#convert-after-import").checked
    ? "A managed copy will be verified first; only then will the selected folder move to Trash."
    : "The original will move to Trash only after the managed copy and output are verified.";
}

function showSourceDetection(source) {
  state.selectedFiles = [];
  state.selectedSource = source;
  $("#import-error").textContent = "";
  $("#dropzone strong").textContent = source.name;
  if (source.kind === "pdf") {
    $("#detected-type").textContent = "Raw PDF · Mokuro conversion required";
    $("#detected-copy").textContent = "The PDF will become page images plus a .mokuro OCR file with selectable Japanese text.";
    $("#convert-after-import").checked = true;
    $("#convert-after-import").disabled = true;
  } else if (source.has_mokuro) {
    $("#detected-type").textContent = "Mokuro-ready · Japanese selectable text";
    $("#detected-copy").textContent = "The existing .mokuro file supplies OCR and title metadata automatically.";
    $("#convert-after-import").checked = false;
    $("#convert-after-import").disabled = true;
  } else if (source.kind === "archive") {
    $("#detected-type").textContent = "Auto-detecting · Bilingual, Mokuro, or Raw";
    $("#detected-copy").textContent = "Cached recognition metadata and archive contents determine its type, language, series, and volume.";
    $("#convert-after-import").disabled = false;
  } else {
    $("#detected-type").textContent = "Raw manga images";
    $("#detected-copy").textContent = "Names and page order are inferred automatically. Mokuro conversion can make Japanese text selectable.";
    $("#convert-after-import").disabled = false;
  }
  $("#import-submit").disabled = false;
  updateDeleteOriginalAvailability();
}

async function chooseOneManga() {
  try {
    const selected = await jsonPost("/api/source/pick", { relink: Boolean(state.relinkVolumeId) });
    if (!selected.cancelled) showSourceDetection(selected);
  } catch (error) {
    $("#import-error").textContent = error.message;
  }
}

function selectFiles(entries) {
  state.selectedSource = null;
  state.selectedFiles = entries.filter(entry => entry.file.size > 0);
  const archives = state.selectedFiles.filter(entry => /\.(pdf|cbz|zip|tar)$/i.test(entry.file.name));
  const loosePages = state.selectedFiles.filter(entry => /\.(jpe?g|png|webp|gif|avif|svg|mokuro)$/i.test(entry.file.name));
  if (!state.relinkVolumeId && archives.length > 1 && !loosePages.length) {
    state.selectedFiles = [];
    $("#dropzone strong").textContent = `${archives.length} separate manga archives detected`;
    $("#import-error").textContent = "This is a collection. Use Scan folder so every edition is recognized, reviewed, and imported separately.";
    $("#import-submit").disabled = true;
    return;
  }
  $("#import-error").textContent = "";
  const total = state.selectedFiles.reduce((sum, entry) => sum + entry.file.size, 0);
  $("#dropzone strong").textContent = `${state.selectedFiles.length} files selected · ${(total / 1024 / 1024).toFixed(1)} MB`;
  const names = state.selectedFiles.map(entry => entry.file.name);
  const isPdf = names.length === 1 && /\.pdf$/i.test(names[0]);
  const hasMokuro = names.some(name => /\.mokuro$/i.test(name));
  const hasLooseImages = names.some(name => /\.(jpe?g|png|webp|gif|avif|svg)$/i.test(name));
  if (isPdf) {
    $("#detected-type").textContent = "Raw PDF · Mokuro conversion required";
    $("#detected-copy").textContent = "Pages, title, volume, and chapter will be found automatically. PDF pages will be rendered and OCR'd locally.";
    $("#convert-after-import").checked = true;
    $("#convert-after-import").disabled = true;
  } else if (hasMokuro) {
    $("#detected-type").textContent = "Mokuro · Japanese with selectable text";
    $("#detected-copy").textContent = "The .mokuro file supplies OCR and title metadata automatically.";
    $("#convert-after-import").checked = false;
    $("#convert-after-import").disabled = true;
  } else if (archives.length === 1 && !hasLooseImages) {
    $("#detected-type").textContent = "Auto-detecting · Bilingual, Mokuro, or Raw";
    $("#detected-copy").textContent = "The local catalogue and archive contents determine its type, language, series, and volume during import.";
    $("#convert-after-import").disabled = false;
  } else {
    $("#detected-type").textContent = "Raw manga images";
    $("#detected-copy").textContent = "Names and page order are inferred automatically. Turn on Mokuro conversion for selectable Japanese text.";
    $("#convert-after-import").disabled = false;
  }
  $("#import-submit").disabled = !state.selectedFiles.length;
  updateDeleteOriginalAvailability();
}

function readDirectory(entry, prefix = "") {
  if (entry.isFile) return new Promise(resolve => entry.file(file => resolve([{ file, path: `${prefix}${file.name}` }])));
  if (!entry.isDirectory) return Promise.resolve([]);
  return new Promise((resolve, reject) => {
    const reader = entry.createReader();
    const all = [];
    const next = () => reader.readEntries(async entries => {
      if (!entries.length) return resolve((await Promise.all(all)).flat());
      all.push(...entries.map(child => readDirectory(child, `${prefix}${entry.name}/`)));
      next();
    }, reject);
    next();
  });
}

function uploadOne(uploadId, entry, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `/api/upload/file?upload_id=${encodeURIComponent(uploadId)}`);
    request.setRequestHeader("X-Relative-Path", encodeURIComponent(entry.path));
    request.upload.onprogress = event => { if (event.lengthComputable) onProgress(event.loaded); };
    request.onload = () => request.status >= 200 && request.status < 300 ? resolve() : reject(new Error(JSON.parse(request.responseText || "{}").error || "Upload failed"));
    request.onerror = () => reject(new Error("Upload connection failed"));
    request.send(entry.file);
  });
}

async function submitImport() {
  const button = $("#import-submit");
  const progress = $("#import-progress");
  const bar = $("div", progress);
  const label = $("span", progress);
  button.disabled = true;
  progress.hidden = false;
  $("#import-error").textContent = "";
  let uploadId = null;
  try {
    const edition = selectedEditionValues();
    const metadata = {
      language: edition.language,
      content_type: edition.content_type,
      series: $("#import-series").value,
      title: $("#import-title").value,
      volume_label: $("#import-volume").value,
      chapter_label: $("#import-chapter").value,
      relink_volume_id: state.relinkVolumeId,
      convert: $("#convert-after-import").checked,
      friendly_names: $("#friendly-names").checked,
      delete_original: $("#delete-original-after-import").checked,
    };
    let result;
    if (state.selectedSource) {
      label.textContent = state.relinkVolumeId ? "Relinking manga…" : "Analyzing pages and matching editions…";
      result = await jsonPost(state.relinkVolumeId ? "/api/relink" : "/api/import", {
        ...metadata,
        path: state.selectedSource.path,
        volume_id: state.relinkVolumeId,
      });
    } else {
      const started = await jsonPost("/api/upload/start", metadata);
    uploadId = started.upload_id;
    const total = state.selectedFiles.reduce((sum, entry) => sum + entry.file.size, 0);
    let completed = 0;
    for (let index = 0; index < state.selectedFiles.length; index++) {
      const entry = state.selectedFiles[index];
      let last = 0;
      label.textContent = `Copying ${index + 1} of ${state.selectedFiles.length}: ${entry.file.name}`;
      await uploadOne(uploadId, entry, loaded => {
        const sent = completed + loaded;
        bar.style.width = `${Math.min(95, sent / total * 95)}%`;
        last = loaded;
      });
      completed += entry.file.size || last;
    }
    bar.style.width = "98%";
    label.textContent = "Analyzing pages and aligning editions…";
      result = await jsonPost("/api/upload/finish", { upload_id: uploadId });
    }
    bar.style.width = "100%";
    $("#import-dialog").close();
    if (!result.pending) await showLibrary();
    queuePairingReview(result.pairing_review);
    if (result.conversion_job) {
      monitorConversion(result.conversion_job).catch(error => toast(error.message, "error"));
      return;
    }
    const recognized = result.bilingual_archive ? `Recognized ${result.detected.series} · ${result.detected.volume_label} (${result.detected.language.toUpperCase()}). ` : "";
    const type = result.content_type ? `${result.content_type[0].toUpperCase()}${result.content_type.slice(1)}` : "Manga";
    toast(result.warning || `${recognized}${type}: ${result.pages} pages added to the library.`, result.warning ? "warning" : "info");
    showNextPairing();
  } catch (error) {
    $("#import-error").textContent = error.message;
    label.textContent = "Import stopped";
    button.disabled = false;
  }
}

async function showSettings() {
  const [status] = await Promise.all([api("/api/status"), state.library.length ? Promise.resolve() : api("/api/library").then(rows => { state.library = rows; })]);
  $("#status-panel").innerHTML = `<div><span>Version</span><b>${escapeHtml(status.version)}</b></div><div><span>Privacy</span><b>Local only · no upload</b></div><div><span>Mokuro conversion</span><b>${status.converter_installed ? "Ready" : "Available on first use"}</b></div><small><b>App data and reading history</b>${escapeHtml(status.data_dir)}</small><small><b>Private manga library</b>${escapeHtml(status.library_dir)}</small>`;
  const series = [...new Set(state.library.map(volume => volume.series))];
  $("#mapping-series").innerHTML = series.map(name => `<option>${escapeHtml(name)}</option>`).join("");
  $("#settings-dialog").showModal();
}

document.addEventListener("click", async event => {
  const target = event.target.closest("[data-action]");
  if (!target) {
    const stage = event.target.closest(".page-stage");
    if (stage) handlePageClick(event, stage);
    return;
  }
  const action = target.dataset.action;
  if (action === "library") { rememberScroll(); showLibrary({ restore: true }); }
  if (action === "add") resetImportDialog();
  if (action === "series") { const fromReader = state.view.kind === "reader"; rememberScroll(); showSeries(target.dataset.series, { restore: fromReader }); }
  if (action === "reader") { rememberScroll(); openReader(target.dataset.id); }
  if (action === "relink") resetImportDialog(target.dataset.id);
  if (action === "remove-volume") removeVolume(target.dataset.id).catch(error => toast(error.message, "error"));
  if (action === "remove-series") removeSeries(target.dataset.series).catch(error => toast(error.message, "error"));
  if (action === "convert") openConversionOptions(target.dataset.id);
  if (action === "confirm-pair") confirmPair(target.dataset.peerId);
  if (action === "reconnect") reconnectLibrary();
  if (action === "reconnect-help") alert("Open Bilingual Manga Reader and Mokuro Converter again. This page will reconnect automatically when the local app is ready. Your library is stored on this computer and is not affected by a closed tab.");
  if (action === "move") movePage(Number(target.dataset.direction));
  if (action === "language") switchLanguage();
  if (action === "bookmark") toggleBookmark().catch(error => toast(error.message, "error"));
  if (action === "direction") { state.direction = state.direction === "rtl" ? "ltr" : "rtl"; jsonPost("/api/settings", { reader_direction: state.direction }).catch(() => {}); renderReader(); }
  if (action === "click-navigation") { state.clickNavigation = !state.clickNavigation; jsonPost("/api/settings", { reader_click_navigation: state.clickNavigation }).catch(() => {}); renderReader(); }
  if (action === "library-filter") { state.libraryFilter = target.dataset.filter; showLibrary(); }
  if (action === "fit") { state.fit = state.fit === "screen" ? "width" : "screen"; jsonPost("/api/settings", { reader_fit: state.fit }).catch(() => {}); renderReader(); }
  if (action === "zoom-in") adjustZoom(0.1);
  if (action === "zoom-out") adjustZoom(-0.1);
  if (action === "fullscreen") document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  if (action === "favorite") toggleFavorite(target.dataset.series);
  if (action === "align") openAlignment(target.dataset.id);
  if (action === "alignment-cancel") openReader(state.mapping.returnId);
  if (action === "map-step") {
    const key = target.dataset.side === "jp" ? "ji" : "ei";
    const volume = state.mapping[target.dataset.side];
    state.mapping[key] = Math.max(0, Math.min(volume.pages.length - 1, state.mapping[key] + Number(target.dataset.direction)));
    renderAlignment();
  }
  if (action === "map-confirm") saveMapping(alignmentPages("jp"), alignmentPages("en"));
  if (action === "map-unmatched") saveMapping(target.dataset.side === "jp" ? alignmentPages("jp") : [], target.dataset.side === "en" ? alignmentPages("en") : []);
});

document.addEventListener("change", event => {
  const target = event.target;
  if (target.dataset.action === "chapter") {
    state.pageIndex = Number(target.value);
    state.displayedIds = [];
    renderReader();
  }
  if (target.dataset.action === "bookmark-jump" && target.value) {
    const index = state.current.pages.findIndex(page => page.id === target.value);
    if (index >= 0) { state.pageIndex = index; state.displayedIds = []; renderReader(); }
  }
  if (target.dataset.action === "ocr-display") {
    state.ocrDisplay = target.value;
    jsonPost("/api/settings", { reader_ocr_display: state.ocrDisplay }).catch(() => {});
    renderReader();
  }
  if (target.id === "library-sort") {
    state.librarySort = target.value;
    showLibrary();
  }
  if (target.dataset.action === "map-spread") {
    state.mapping[`${target.dataset.side}Count`] = target.checked ? 2 : 1;
    renderAlignment();
  }
});

$("#home-button").addEventListener("click", () => showLibrary());
$("#add-button").addEventListener("click", () => resetImportDialog());
$("#settings-button").addEventListener("click", () => showSettings().catch(error => toast(error.message, "error")));
$("#search").addEventListener("input", showLibrary);
$("#bulk-pick").addEventListener("click", beginBulkScan);
$("#bulk-import").addEventListener("click", importBulkSelection);
$("#bulk-select-ready").addEventListener("click", () => {
  for (const item of state.bulk.job?.items || []) item.selected = item.status === "ready";
  renderBulkResults();
});
$("#bulk-clear").addEventListener("click", () => {
  for (const item of state.bulk.job?.items || []) item.selected = false;
  renderBulkResults();
});
$("#bulk-filter").addEventListener("change", event => {
  state.bulk.filter = event.target.value;
  renderBulkResults();
});
$("#bulk-search").addEventListener("input", event => {
  state.bulk.query = event.target.value;
  renderBulkResults();
});
function syncBulkItem(event) {
  const row = event.target.closest("[data-bulk-id]");
  if (!row) return;
  const item = state.bulk.job?.items.find(candidate => candidate.id === row.dataset.bulkId);
  if (!item) return;
  if (event.target.matches("[data-bulk-select]")) item.selected = event.target.checked;
  const field = event.target.dataset.bulkField;
  if (field === "edition_type") Object.assign(item, editionValues(event.target.value));
  else if (field) item[field] = event.target.value;
  updateBulkSummary();
}
$("#bulk-list").addEventListener("input", syncBulkItem);
$("#bulk-list").addEventListener("change", syncBulkItem);
$("#choose-manga").addEventListener("click", chooseOneManga);
$("#folder-input").addEventListener("change", event => selectFiles([...event.target.files].map(file => ({ file, path: file.webkitRelativePath || file.name }))));
$("#archive-input").addEventListener("change", event => selectFiles([...event.target.files].map(file => ({ file, path: file.name }))));
$("#import-type").addEventListener("change", event => {
  if (event.target.value.endsWith("-en")) {
    $("#convert-after-import").checked = false;
    $("#convert-after-import").disabled = true;
  } else if (!state.selectedFiles.some(entry => /\.(pdf|mokuro)$/i.test(entry.file.name)) && state.selectedSource?.kind !== "pdf" && !state.selectedSource?.has_mokuro) {
    $("#convert-after-import").disabled = false;
  }
  updateDeleteOriginalAvailability();
});
$("#convert-after-import").addEventListener("change", updateDeleteOriginalAvailability);
$("#import-submit").addEventListener("click", submitImport);
$("#remove-confirm").addEventListener("click", confirmRemoval);
$("#conversion-start").addEventListener("click", () => {
  if (!state.conversionVolumeId) return;
  convertVolume(state.conversionVolumeId, $("#conversion-delete-original").checked).catch(error => {
    $("#conversion-options-error").textContent = error.message;
  });
});
$("#conversion-done").addEventListener("click", event => {
  const volumeId = event.currentTarget.dataset.id;
  if (volumeId) openReader(volumeId);
});

const dropzone = $("#dropzone");
for (const name of ["dragenter", "dragover"]) dropzone.addEventListener(name, event => { event.preventDefault(); dropzone.classList.add("active"); });
for (const name of ["dragleave", "drop"]) dropzone.addEventListener(name, event => { event.preventDefault(); dropzone.classList.remove("active"); });
dropzone.addEventListener("drop", async event => {
  const entries = [...event.dataTransfer.items].map(item => item.webkitGetAsEntry?.()).filter(Boolean);
  if (entries.length) selectFiles((await Promise.all(entries.map(entry => readDirectory(entry)))).flat());
  else selectFiles([...event.dataTransfer.files].map(file => ({ file, path: file.name })));
});
dropzone.addEventListener("keydown", event => {
  if (event.key !== "Enter" && event.key !== " ") return;
  chooseOneManga();
});

document.querySelectorAll("[data-folder]").forEach(button => button.addEventListener("click", () => jsonPost("/api/open-folder", { target: button.dataset.folder }).catch(error => toast(error.message, "error"))));
$("#mapping-import").addEventListener("click", async () => {
  const file = $("#mapping-file").files[0];
  if (!file) return toast("Choose a mapping JSON file first.", "warning");
  try {
    const result = await jsonPost("/api/mappings/import", { series: $("#mapping-series").value, data: JSON.parse(await file.text()) });
    toast(`Imported ${result.inserted} mappings · ${result.review} need review · ${result.unresolved} unresolved`);
  } catch (error) { toast(error.message, "error"); }
});
$("#quit-button").addEventListener("click", async () => {
  if (!confirm("Quit Bilingual Manga Reader and Mokuro Converter? You can reopen the app at any time.")) return;
  await jsonPost("/api/shutdown", {});
  $("#settings-dialog").close();
  app.innerHTML = '<section class="empty"><h1>Bilingual Manga Reader and Mokuro Converter has quit</h1><p>You can close this tab and reopen the app whenever you are ready.</p></section>';
});

$("#pairing-dialog").addEventListener("close", () => {
  state.pairingReview = null;
  showNextPairing();
});

document.addEventListener("keydown", event => {
  if (event.target.closest("input,select,textarea,dialog")) return;
  if (!state.current || state.mapping) return;
  const key = event.key.toLowerCase();
  if (key === "l") switchLanguage();
  else if (key === "a") openAlignment(state.current.id);
  else if (key === "b") toggleBookmark().catch(error => toast(error.message, "error"));
  else if (key === "f") toggleFavorite(state.current.series);
  else if (event.key === "ArrowLeft") movePage(state.direction === "rtl" ? 1 : -1);
  else if (event.key === "ArrowRight") movePage(state.direction === "rtl" ? -1 : 1);
  else if (event.key === "+" || event.key === "=") adjustZoom(0.1);
  else if (event.key === "-") adjustZoom(-0.1);
  else if (event.key === "0") adjustZoom(0);
});

async function bootstrap() {
  const settings = await api("/api/settings").catch(() => ({}));
  if (["screen", "width"].includes(settings.reader_fit)) state.fit = settings.reader_fit;
  if (Number(settings.reader_zoom) >= 0.5 && Number(settings.reader_zoom) <= 3) state.zoom = Number(settings.reader_zoom);
  if (["rtl", "ltr"].includes(settings.reader_direction)) state.direction = settings.reader_direction;
  if (["hover", "visible", "hidden"].includes(settings.reader_ocr_display)) state.ocrDisplay = settings.reader_ocr_display;
  if (typeof settings.reader_click_navigation === "boolean") state.clickNavigation = settings.reader_click_navigation;
  await showLibrary();
}

bootstrap();

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && $(".error-card")) reconnectLibrary();
});
