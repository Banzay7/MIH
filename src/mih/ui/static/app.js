// MIRTVONLINE web UI: a small hash-routed app over /api/v1. No build step, no libraries.
// Every value from the API is escaped by the html`` tag: names and titles come from
// playlists and guides, which are untrusted.
"use strict";

const API = "/api/v1";
const PROFILE = "default";
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const view = document.getElementById("view");

// ---------- HTML helpers ----------

class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = (s) => new Raw(s);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
function html(strings, ...values) {
  let out = strings[0];
  values.forEach((v, i) => {
    if (Array.isArray(v)) out += v.map((x) => (x instanceof Raw ? x.s : esc(x))).join("");
    else out += v instanceof Raw ? v.s : esc(v);
    out += strings[i + 1];
  });
  return raw(out);
}
const enc = encodeURIComponent;

// ---------- Storage (per browser; failures are fine) ----------

const store = {
  get(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// What the visitor chose in Settings; each read falls back to the default.
const PAGE_SIZES = [20, 50, 100];
const RELEVANCE = [["0", "Все совпадения"], ["0.3", "Хорошие совпадения"], ["0.7", "Только лучшие"]];
const prefs = {
  pageSize: () => (PAGE_SIZES.includes(Number(store.get("mih.pageSize"))) ? Number(store.get("mih.pageSize")) : 50),
  nowSize: () => (PAGE_SIZES.includes(Number(store.get("mih.nowSize"))) ? Number(store.get("mih.nowSize")) : 50),
  strict: () => store.get("mih.strict") === "1",
  relevance: () => (RELEVANCE.some(([v]) => v === store.get("mih.relevance")) ? store.get("mih.relevance") : "0"),
  playableOnly: () => store.get("mih.playable") === "1",
  logos: () => store.get("mih.logos", "1") === "1",
};

// ---------- API ----------

class ApiFailure extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  const token = store.get("mih.token");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(API + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 204) return null;
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  if (!resp.ok) {
    const err = data?.error;
    throw new ApiFailure(resp.status, err?.code || (resp.status === 401 ? "unauthorized" : "http"),
      err?.message || data?.detail || resp.statusText);
  }
  return data;
}

// ---------- Shared state ----------

const state = {
  channels: new Map(), // id -> {id, canonical_name, logo_url, country, has_epg, streams}
  channelsLoaded: false,
  favorites: new Set(), // "channel:<id>" | "programme:<id>"
  favoritesLoaded: false,
};

async function loadChannels() {
  if (state.channelsLoaded) return;
  // One request for the whole catalog (gzip makes it small); pages only if it grows past it.
  for (let offset = 0; ; offset += 10000) {
    const page = await api(`/channels?limit=10000&offset=${offset}`);
    page.items.forEach((c) => state.channels.set(c.id, c));
    if (page.items.length < 10000) break;
  }
  state.channelsLoaded = true;
}

async function loadFavorites() {
  const data = await api(`/profiles/${PROFILE}/favorites`);
  state.favorites = new Set(data.items.map((f) => `${f.target_kind}:${f.target_id}`));
  state.favoritesLoaded = true;
  return data.items;
}

const isFav = (kind, id) => state.favorites.has(`${kind}:${id}`);

async function toggleFav(kind, id) {
  const on = isFav(kind, id);
  if (on) {
    await api(`/profiles/${PROFILE}/favorites/${kind}/${enc(id)}`, { method: "DELETE" });
    state.favorites.delete(`${kind}:${id}`);
  } else {
    await api(`/profiles/${PROFILE}/favorites`, { method: "POST", body: { target_kind: kind, target_id: id } });
    state.favorites.add(`${kind}:${id}`);
  }
  document.querySelectorAll(`.star[data-kind="${kind}"][data-id="${CSS.escape(id)}"]`).forEach((b) => {
    b.classList.toggle("on", !on);
    b.setAttribute("aria-pressed", String(!on));
    b.title = !on ? "Убрать из избранного" : "В избранное";
  });
  toast(on ? "Убрано из избранного" : "Добавлено в избранное");
}

// ---------- Playback ("На ТВ") ----------

const PLAY_TEXT = {
  accepted: "Передаю команду агенту…",
  taken: "Агент включает канал на ТВ",
  all_failed: "Ни один поток этого канала не заиграл на ТВ.",
  started: "Запущено на ТВ",
  no_adapter: "Поток выбран, но запуск на ТВ выключен на сервере (MIH_PLAYBACK=off).",
  no_agent: "Агент не взял команду: он не подключён к MCP или не проверяет очередь.",
  no_stream: "У этого канала сейчас нет рабочего потока.",
  ha_started: "Home Assistant выполнил скрипт: канал включается на ТВ",
  ha_unreachable: "Home Assistant недоступен с сервера: проверьте MIH_HA_URL и связь (Tailscale).",
  ha_unauthorized: "Home Assistant не принял токен: создайте новый и пропишите в MIH_HA_TOKEN.",
  ha_script_not_found: "В Home Assistant нет такого скрипта: проверьте MIH_HA_SCRIPT.",
  ha_script_failed: "Скрипт Home Assistant завершился с ошибкой. Подробности в журнале HA.",
  ha_timeout: "Home Assistant не ответил вовремя. Канал, возможно, всё же включится.",
  ha_not_playing: "Скрипт Home Assistant сообщил, что канал не включился",
};

// "agent" or "ha": who starts the TV from this browser; none = the server's default.
const playAdapter = () => store.get("mih.adapter");

async function playOnTv(channelId, feedId, button) {
  if (button) button.disabled = true;
  try {
    const key = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    const target = { kind: "channel", channel_id: channelId };
    if (feedId) target.feed_id = feedId;
    const body = { target, idempotency_key: key };
    if (playAdapter()) body.adapter = playAdapter();
    let cmd = await api("/playback/commands", { method: "POST", body });
    // The agent has 30 s to take the command; then silence for 60 s means it plays,
    // so once it is taken the toast says so and only a failure changes it.
    let told = false;
    for (let i = 0; cmd.state === "accepted" && i < 50; i++) {
      if (!told) toast(cmd.taken ? PLAY_TEXT.taken : PLAY_TEXT.accepted, cmd.taken ? 6000 : 3000);
      if (cmd.taken) { told = true; if (button) button.disabled = false; }
      await new Promise((r) => setTimeout(r, 2000));
      cmd = await api(`/playback/commands/${enc(cmd.id)}`);
    }
    if (told && cmd.state === "started") return;
    if (cmd.state === "started") toast(cmd.adapter === "ha" ? PLAY_TEXT.ha_started : PLAY_TEXT.started);
    else if (cmd.state === "failed") {
      const text = PLAY_TEXT[cmd.error] || `Не удалось запустить: ${cmd.error}`;
      toast(cmd.detail ? `${text}: ${cmd.detail}` : text, 8000);
    }
    else toast("ТВ не ответил вовремя. Попробуйте ещё раз.");
  } catch (e) {
    toast(errorText(e), 6000);
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------- Formatting ----------

const fmtTime = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const fmtDay = new Intl.DateTimeFormat("ru-RU", { weekday: "short", day: "numeric", month: "short" });
const time = (iso) => fmtTime.format(new Date(iso));

function dayLabel(d) {
  const today = startOfDay(new Date());
  const diff = Math.round((startOfDay(d) - today) / 86400000);
  if (diff === 0) return "Сегодня";
  if (diff === 1) return "Завтра";
  if (diff === -1) return "Вчера";
  return fmtDay.format(d);
}

function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }

function whenText(a) {
  const s = new Date(a.start), now = Date.now();
  const range = `${time(a.start)}–${time(a.stop)}`;
  if (s <= now && new Date(a.stop) > now) return html`<span class="live">● В эфире</span> ${range}`;
  const day = dayLabel(s);
  return `${day === "Сегодня" ? "" : day + ", "}${range}`;
}

function progress(a) {
  const s = Date.parse(a.start), e = Date.parse(a.stop), now = Date.now();
  if (!(s <= now && now < e)) return "";
  const pct = Math.max(0, Math.min(100, ((now - s) / (e - s)) * 100));
  return html`<div class="progress" aria-hidden="true"><i style="width:${pct.toFixed(1)}%"></i></div>`;
}

const LANGS = { ru: "русский", en: "английский", vi: "вьетнамский" };
const FIELDS = { title: "в названии", sub_title: "в подзаголовке", description: "в описании",
  categories: "в жанре", channel_name: "в названии канала", time: "время", language: "язык",
  topic: "тема", programme: "в избранном" };
const TIMES = { now: "сейчас", today: "сегодня", tomorrow: "завтра", tonight: "вечером",
  yesterday: "вчера", custom: "свой интервал" };

function initials(name) {
  return (name || "?").replace(/[^\p{L}\p{N} ]/gu, "").split(/\s+/).filter(Boolean)
    .slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
}

function logo(channelId, name, big = false, logoUrl = null) {
  const url = logoUrl || state.channels.get(channelId)?.logo_url;
  const cls = big ? "logo big" : "logo";
  if (url && /^https?:\/\//i.test(url) && prefs.logos()) {
    return html`<img class="${cls}" src="${url}" alt="" loading="lazy" referrerpolicy="no-referrer"
      data-fallback="${initials(name)}">`;
  }
  return html`<span class="${cls}" aria-hidden="true">${initials(name)}</span>`;
}

function star(kind, id) {
  const on = isFav(kind, id);
  return html`<button class="star ${on ? "on" : ""}" data-kind="${kind}" data-id="${id}"
    aria-pressed="${on}" title="${on ? "Убрать из избранного" : "В избранное"}"
    aria-label="Избранное">★</button>`;
}

function tvButton(channelId, feedId) {
  return html`<button class="btn tv" data-play="${channelId}" data-feed="${feedId || ""}">На ТВ</button>`;
}

function errorText(e) {
  if (e instanceof ApiFailure) {
    if (e.status === 401) return "Нужен токен доступа: укажите его в настройках.";
    if (e.code === "not_found") return "Не найдено.";
    return `Ошибка сервера: ${e.message}`;
  }
  return "Сервер недоступен. Проверьте соединение.";
}

function errorBox(e) {
  const needToken = e instanceof ApiFailure && e.status === 401;
  return html`<div class="error"><p>${errorText(e)}</p>
    ${needToken ? html`<p><a class="btn" href="#/settings">Открыть настройки</a></p>` : ""}</div>`;
}

const skeleton = (n = 4) => html`<div class="grid">${Array.from({ length: n }, () => html`<div class="skeleton"></div>`)}</div>`;

let toastTimer;
function toast(text, ms = 3000) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

// ---------- Paged lists: the first page, the rest on "Показать ещё" ----------

// A long list renders one page; the button appends the next page in place, so the
// screen neither reloads nor jumps to the top. fetchMore(offset, size) asks the server
// for lists it pages itself (search); other lists are already in memory.
const pagers = new Map();
let pagerSeq = 0;

function paged(items, card, { total = items.length, fetchMore = null, size = prefs.pageSize() } = {}) {
  const id = `list-${++pagerSeq}`;
  const first = items.slice(0, size);
  pagers.set(id, { items, card, total, fetchMore, size, shown: first.length });
  return html`<div class="grid" id="${id}">${first.map(card)}</div>${moreButton(id)}`;
}

function moreButton(id) {
  const p = pagers.get(id);
  const left = p.total - p.shown;
  if (left <= 0) return "";
  const next = Math.min(left, p.size);
  return html`<button class="btn more" type="button" data-more="${id}">Показать ещё ${next} · осталось ${nf.format(left)}</button>`;
}

async function showMore(btn) {
  const id = btn.dataset.more;
  const p = pagers.get(id);
  const list = document.getElementById(id);
  if (!p || !list) return;
  const size = p.size;
  btn.disabled = true;
  try {
    if (p.fetchMore && p.items.length < Math.min(p.total, p.shown + size)) {
      const more = await p.fetchMore(p.items.length, size);
      p.items.push(...more);
      if (!more.length) p.total = p.items.length; // the server has fewer than it said
    }
    const chunk = p.items.slice(p.shown, p.shown + size);
    list.insertAdjacentHTML("beforeend", chunk.map((x) => String(p.card(x))).join(""));
    p.shown += chunk.length;
    btn.outerHTML = String(moreButton(id));
  } catch (e) {
    btn.disabled = false;
    toast(errorText(e), 5000);
  }
}

const refreshButton = () => html`<button class="btn" type="button" data-refresh title="Загрузить свежие данные">↻ Обновить</button>`;
const heading = (title) => html`<div class="head"><h1>${title}</h1>${refreshButton()}</div>`;

// ---------- Cards ----------

function nowCard(item) {
  const name = item.channel_name || state.channels.get(item.channel_id)?.canonical_name || "Канал";
  const cur = item.now, nxt = item.next;
  return html`<article class="card">
    ${logo(item.channel_id, name, false, item.logo_url)}
    <div class="body">
      <a class="stretch title" href="#/channel/${enc(item.channel_id)}">${name}</a>
      ${cur ? html`<div class="line">${cur.title}</div>${progress(cur)}
        <div class="small muted">${time(cur.start)}–${time(cur.stop)}</div>`
        : html`<div class="muted small">Сейчас нет данных о программе</div>`}
      ${nxt ? html`<div class="small muted line">Далее ${time(nxt.start)} · ${nxt.title}</div>` : ""}
    </div>
    <div class="actions">${star("channel", item.channel_id)}${tvButton(item.channel_id, item.feed_id)}</div>
  </article>`;
}

function airingCard(a) {
  const name = a.channel_name || state.channels.get(a.channel_id)?.canonical_name || "";
  const why = (a.why_matched || []).filter((w) => w.field !== "time" && w.field !== "language");
  return html`<article class="card">
    ${logo(a.channel_id, name, false, a.logo_url)}
    <div class="body">
      <a class="stretch title" href="#/programme/${enc(a.programme_id)}">${a.title}</a>
      ${a.sub_title ? html`<div class="small line">${a.sub_title}</div>` : ""}
      <div class="small muted">${whenText(a)} · ${name}</div>
      ${progress(a)}
      <div class="chips">
        ${why.map((w) => html`<span class="chip" title="Почему найдено">${FIELDS[w.field] || w.field}: ${w.matched && w.matched !== "substring" ? w.matched : w.term}</span>`)}
        ${a.lang ? html`<span class="chip">${LANGS[a.lang] || a.lang}</span>` : ""}
        ${a.more_airings ? html`<span class="chip">ещё показов: ${a.more_airings}</span>` : ""}
        ${a.streams_available === 0 ? html`<span class="chip">нет рабочих потоков</span>` : ""}
      </div>
    </div>
    <div class="actions">${star("programme", a.programme_id)}${tvButton(a.channel_id, a.feed_id)}</div>
  </article>`;
}

function channelCard(c) {
  const id = c.id || c.channel_id;
  const name = c.canonical_name || c.channel_name;
  const streams = c.streams ?? c.streams_available;
  const facts = [c.country, c.has_epg === false ? "без программы" : c.has_epg ? "есть программа" : null,
    streams === 0 ? "нет рабочих потоков" : null].filter(Boolean);
  return html`<article class="card">
    ${logo(id, name)}
    <div class="body">
      <a class="stretch title" href="#/channel/${enc(id)}">${name}</a>
      <div class="small muted">${facts.join(" · ")}</div>
    </div>
    <div class="actions">${star("channel", id)}${tvButton(id)}</div>
  </article>`;
}

// ---------- Views ----------

// The server cuts the guide into pages: the screen asks for one page of "on air", the
// next few starts and the favourites, not the whole guide or the whole catalog.
async function viewHome() {
  render(html`<h1>Сейчас в эфире</h1>${skeleton()}`);
  const size = prefs.nowSize();
  const playable = prefs.playableOnly() ? "&playable_only=true" : "";
  const [, data, soon] = await Promise.all([loadFavorites(),
    api(`/now?on_air=true&limit=${size}${playable}`), api(`/now?soon=45&limit=12${playable}`)]);
  if (!data.channels) {
    return html`<h1>Сейчас в эфире</h1>
      <div class="empty"><p>Каталог пока пуст.</p>
      <p class="small">Добавьте свой плейлист и программу передач (EPG): командой
      <code>mih import-file</code> или через API, см. <a href="/api/docs">документацию API</a>.</p></div>`;
  }
  const favIds = [...state.favorites].filter((k) => k.startsWith("channel:")).map((k) => k.slice(8));
  const favNow = favIds.length
    ? (await api(`/now?on_air=true&${favIds.map((id) => `channel_id=${enc(id)}`).join("&")}`)).items : [];
  const noGuide = data.channels - data.guided;
  const more = async (offset, limit) => (await api(`/now?on_air=true&limit=${limit}&offset=${offset}${playable}`)).items;
  return html`
    ${heading("Сейчас в эфире")}
    <p class="muted small">${nf.format(data.total)} в эфире · ${nf.format(data.channels)} каналов в каталоге${noGuide > 0 ? ` · без программы: ${nf.format(noGuide)}` : ""}</p>
    ${favNow.length ? html`<h2>Избранное</h2><div class="grid">${favNow.map(nowCard)}</div>` : ""}
    ${soon.items.length ? html`<h2>Скоро начнётся</h2><div class="grid">${soon.items.map((i) => airingCard({
      ...i.next, channel_name: i.channel_name, logo_url: i.logo_url }))}</div>` : ""}
    <h2>Все каналы в эфире${data.total ? ` · ${nf.format(data.total)}` : ""}</h2>
    ${data.total ? paged(data.items, nowCard, { total: data.total, fetchMore: more, size })
      : html`<div class="empty">Нет данных о текущем эфире. Загрузите программу передач (EPG).</div>`}`;
}

const SEARCH_TIMES = [["", "Любое время"], ["now", "Сейчас"], ["today", "Сегодня"], ["tonight", "Вечером"], ["tomorrow", "Завтра"]];
const SEARCH_LANGS = [["", "Любой язык"], ["ru", "Русский"], ["en", "Английский"], ["vi", "Вьетнамский"]];
const SEARCH_TYPES = [["", "Всё"], ["airing", "Передачи"], ["channel", "Каналы"]];
const SEARCH_MODES = [["0", "Обычный поиск"], ["1", "Строгий: целые слова"]];
const SEARCH_KEYS = ["time", "lang", "type", "strict"];

function chipRow(name, options, current) {
  return html`<div class="row" role="group">${options.map(([v, label]) =>
    html`<button type="button" class="chip ${v === current ? "on" : ""}" data-filter="${name}" data-value="${v}"
      aria-pressed="${v === current}">${label}</button>`)}</div>`;
}

// Filters only change the choice; the search runs on Enter or the "Найти" button.
async function viewSearch(params) {
  const q = params.get("q") || "";
  const timeF = params.get("time") || "";
  const lang = params.get("lang") || "";
  const type = params.get("type") || "";
  const strict = params.get("strict") || (prefs.strict() ? "1" : "0");
  document.getElementById("search-input").value = q;
  const filters = html`<div class="filters">
    ${chipRow("time", SEARCH_TIMES, timeF)}${chipRow("lang", SEARCH_LANGS, lang)}${chipRow("type", SEARCH_TYPES, type)}
    ${chipRow("strict", SEARCH_MODES, strict)}
    <div class="row"><button class="btn tv" type="button" id="search-run">Найти</button>
      <span class="small muted">или Enter в строке поиска</span></div>
  </div>`;
  if (!q && !timeF) {
    return html`<h1>Поиск</h1>${filters}
      <div class="empty"><p>Ищите передачи и каналы обычными словами.</p>
      <p class="small">Например: «новости вечером», «футбол завтра», «роботы на русском», «Первый канал».</p></div>`;
  }
  render(html`<h1>Поиск</h1>${filters}${skeleton(3)}`);
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const body = { query: q, tz: TZ, limit: prefs.pageSize(), filters: {}, strict: strict === "1",
    min_relevance: Number(prefs.relevance()), playable_only: prefs.playableOnly() };
  if (timeF) body.filters.time = timeF;
  if (lang) body.filters.languages = [lang];
  if (type) body.target_types = [type];
  const res = await api("/search", { method: "POST", body });
  const total = res.total || {};
  const more = (kind) => async (offset, limit) =>
    (await api("/search", { method: "POST", body: { ...body, target_types: [kind], offset, limit } }))
      .items.filter((i) => i.type === kind);
  const pq = res.query;
  const understood = [
    pq.terms.length ? `слова: ${pq.terms.join(", ")}` : null,
    pq.time ? `время: ${TIMES[pq.time] || pq.time}` : null,
    pq.languages.length ? `язык: ${pq.languages.map((l) => LANGS[l] || l).join(", ")}` : null,
  ].filter(Boolean);
  const channels = res.items.filter((i) => i.type === "channel");
  const airings = res.items.filter((i) => i.type === "airing");
  return html`<h1>Поиск</h1>${filters}
    ${understood.length ? html`<p class="small muted">Понял запрос так: ${understood.join(" · ")}</p>` : ""}
    ${pq.terms.length ? html`<p><button class="btn" type="button" data-follow="${pq.terms.join(" ")}">🔔 Следить за темой «${pq.terms.join(" ")}»</button></p>` : ""}
    ${channels.length ? html`<h2>Каналы · ${nf.format(total.channel ?? channels.length)}</h2>
      ${paged(channels, channelCard, { total: total.channel ?? channels.length, fetchMore: more("channel") })}` : ""}
    ${airings.length ? html`<h2>Передачи · ${nf.format(total.airing ?? airings.length)}</h2>
      ${paged(airings, airingCard, { total: total.airing ?? airings.length, fetchMore: more("airing") })}` : ""}
    ${!res.items.length ? html`<div class="empty">Ничего не нашлось. Попробуйте другие слова или уберите фильтры.</div>` : ""}`;
}

async function viewChannels(params) {
  const q = params.get("q") || "";
  const epg = params.get("epg") === "1";
  const country = params.get("country") || "";
  const playable = prefs.playableOnly();
  render(html`<h1>Каналы</h1>${skeleton(6)}`);
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const all = [...state.channels.values()];
  const countries = [...new Set(all.map((c) => c.country).filter(Boolean))].sort();
  const needle = q.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
  const list = all.filter((c) => (!epg || c.has_epg) && (!country || c.country === country) &&
    (!playable || c.streams !== 0) &&
    (!needle || c.canonical_name.toLocaleLowerCase("ru").replaceAll("ё", "е").includes(needle)));
  return html`<h1>Каналы</h1>
    <form class="filters row" id="channel-filter">
      <input name="q" type="search" value="${q}" placeholder="Название канала" aria-label="Фильтр по названию">
      ${countries.length ? html`<select name="country" aria-label="Страна">
        <option value="">Все страны</option>
        ${countries.map((c) => html`<option value="${c}" ${c === country ? raw("selected") : ""}>${c}</option>`)}
      </select>` : ""}
      <label class="row small"><input type="checkbox" name="epg" value="1" ${epg ? raw("checked") : ""}> только с программой</label>
      <div class="row"><button class="btn tv" type="submit">Показать</button></div>
    </form>
    <p class="small muted">Найдено: ${nf.format(list.length)}${playable ? " · без рабочих потоков скрыты (см. настройки)" : ""}</p>
    ${list.length ? paged(list, channelCard) : html`<div class="empty">Нет каналов под эти условия.</div>`}`;
}

function feedLabel(f) {
  const parts = [];
  if (f.region) parts.push(f.region);
  if (f.offset_minutes) parts.push(`${f.offset_minutes > 0 ? "+" : ""}${f.offset_minutes / 60} ч`);
  if (f.audio_lang) parts.push(LANGS[f.audio_lang] || f.audio_lang);
  return parts.join(", ") || "Основной";
}

async function viewChannel(id, params) {
  render(skeleton(2));
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const ch = await api(`/channels/${enc(id)}`);
  const feeds = ch.feeds;
  const feedId = params.get("feed") || feeds.find((f) => f.offset_minutes === 0 && f.epg.available)?.id
    || feeds.find((f) => f.epg.available)?.id || feeds[0]?.id;
  const feed = feeds.find((f) => f.id === feedId);
  const dayOffset = Math.max(-1, Math.min(6, Number(params.get("day") || 0)));
  const from = startOfDay(new Date());
  from.setDate(from.getDate() + dayOffset);
  const to = new Date(from);
  to.setDate(to.getDate() + 1);

  let schedule = [];
  if (feed?.epg.available) {
    const res = await api(`/airings?channel_id=${enc(id)}&from=${enc(from.toISOString())}&to=${enc(to.toISOString())}`);
    schedule = res.feeds.find((f) => f.feed_id === feed.id)?.airings || [];
  }
  const playable = feeds.flatMap((f) => f.streams).filter((s) => !s.availability_reason &&
    !["quarantine", "archived"].includes(s.status)).length;
  const now = Date.now();
  const days = [-1, 0, 1, 2, 3, 4, 5, 6].map((d) => {
    const x = startOfDay(new Date()); x.setDate(x.getDate() + d); return [d, dayLabel(x)];
  });
  const link = (extra) => "#/channel/" + enc(id) + "?" + new URLSearchParams({ feed: feedId || "", day: String(dayOffset), ...extra });
  return html`
    <div class="hero">
      ${logo(id, ch.canonical_name, true)}
      <div class="body">
        <h1>${ch.canonical_name}</h1>
        <div class="muted small">${[ch.country, `потоков: ${playable}`].filter(Boolean).join(" · ")}</div>
      </div>
      <div class="row">${star("channel", id)}${tvButton(id, feedId)}</div>
    </div>
    ${feeds.length > 1 ? html`<div class="row" role="group" aria-label="Версия канала">${feeds.map((f) =>
      html`<a class="chip ${f.id === feedId ? "on" : ""}" href="${link({ feed: f.id })}">${feedLabel(f)}</a>`)}</div>` : ""}
    <h2>Программа</h2>
    ${feed?.epg.available ? html`
      <div class="row" role="group" aria-label="День">${days.map(([d, label]) =>
        html`<a class="chip ${d === dayOffset ? "on" : ""}" href="${link({ day: String(d) })}">${label}</a>`)}</div>
      <p></p>
      ${schedule.length ? html`<ol class="schedule">${schedule.map((a) => {
        const s = Date.parse(a.start), e = Date.parse(a.stop);
        const cls = s <= now && now < e ? "now" : e <= now ? "past" : "";
        return html`<li class="${cls}"><time datetime="${a.start}">${time(a.start)}</time>
          <a href="#/programme/${enc(a.programme_id)}"><div>${a.title}${cls === "now" ? html` <span class="live small">● в эфире</span>` : ""}</div>
          ${a.sub_title ? html`<div class="small muted">${a.sub_title}</div>` : ""}
          ${cls === "now" ? progress(a) : ""}</a></li>`;
      })}</ol>` : html`<div class="empty">На этот день программы нет.</div>`}`
      : html`<div class="empty">Для этого канала нет программы передач. Её можно привязать в очереди EPG.</div>`}
    ${ch.aliases.length ? html`<h2>Другие названия</h2><div class="chips">${[...new Set(ch.aliases.map((a) => a.display).filter(Boolean))]
      .map((n) => html`<span class="chip">${n}</span>`)}</div>` : ""}`;
}

async function viewProgramme(id) {
  render(skeleton(2));
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const p = await api(`/programmes/${enc(id)}`);
  const now = Date.now();
  const upcoming = p.airings.filter((a) => Date.parse(a.stop) > now);
  const meta = [p.episode_num, p.lang ? LANGS[p.lang] || p.lang : null, p.rating].filter(Boolean);
  const other = p.titles.filter((t) => t.text !== p.title);
  return html`
    <div class="hero">
      ${p.icon_url && /^https?:\/\//i.test(p.icon_url) && prefs.logos()
        ? html`<img class="logo big" src="${p.icon_url}" alt="" referrerpolicy="no-referrer" data-fallback="">` : ""}
      <div class="body">
        <h1>${p.title}</h1>
        ${p.sub_title ? html`<div>${p.sub_title}</div>` : ""}
        ${meta.length ? html`<div class="small muted">${meta.join(" · ")}</div>` : ""}
      </div>
      <div class="row">${star("programme", id)}</div>
    </div>
    ${p.categories.length ? html`<div class="chips">${p.categories.map((c) => html`<span class="chip">${c}</span>`)}</div>` : ""}
    ${p.description ? html`<p class="description">${p.description}</p>` : ""}
    ${other.length ? html`<p class="small muted">Также: ${other.map((t) => t.text).join(" / ")}</p>` : ""}
    <h2>Когда покажут</h2>
    ${upcoming.length ? html`<div class="grid">${upcoming.map((a) => {
      const name = state.channels.get(a.channel_id)?.canonical_name || "";
      return html`<article class="card">${logo(a.channel_id, name)}
        <div class="body"><a class="stretch title" href="#/channel/${enc(a.channel_id)}">${name}</a>
        <div class="small muted">${whenText(a)}${a.timeshift_minutes ? ` · +${a.timeshift_minutes / 60} ч` : ""}</div>${progress(a)}</div>
        <div class="actions">${tvButton(a.channel_id, a.feed_id)}</div></article>`;
    })}</div>` : html`<div class="empty">Ближайших показов нет.</div>`}`;
}

// ---------- Topics: pushes before a programme on a followed topic starts ----------

async function followTopic(topic) {
  const r = await api(`/profiles/${PROFILE}/topics`, { method: "POST", body: { topic } });
  toast(`Тема «${r.topic}» добавлена: пуш придёт перед началом передачи`, 4000);
}

function alertCard(m) {
  const ch = m.channels[0];
  return airingCard({
    programme_id: m.programme_id, title: m.title, sub_title: m.sub_title, start: m.start,
    stop: m.stop, channel_id: ch.channel_id,
    channel_name: m.channels.length > 1 ? `${ch.name} и ещё ${m.channels.length - 1}` : ch.name,
    why_matched: m.reasons.map((r) => ({ field: r.kind, term: r.kind === "topic" ? r.value : "передача" })),
  });
}

function topicsBlock(topics, upcoming, total = upcoming.length) {
  return html`<h2>Темы</h2>
    <p class="small muted">Перед началом передачи по теме придёт пуш со ссылкой на запуск. Тема ищется в названии, подзаголовке и жанре передачи, с начала слова.</p>
    ${topics.length ? html`<div class="chips">${topics.map((t) => html`<button type="button" class="chip on" data-topic-del="${t.topic}"
      title="Убрать тему" aria-label="Убрать тему ${t.topic}">${t.topic} ×</button>`)}</div>` : ""}
    <form id="topic-form" class="row field">
      <input name="topic" minlength="2" maxlength="64" required autocomplete="off" aria-label="Новая тема"
        placeholder="Например: футбол, формула 1, роботы">
      <button class="btn tv" type="submit">Следить</button>
    </form>
    ${upcoming.length ? html`<h2>Скоро по вашим темам${total > upcoming.length ? ` · первые ${upcoming.length} из ${nf.format(total)}` : ""}</h2><div class="grid">${upcoming.map(alertCard)}</div>`
      : topics.length ? html`<p class="muted small">В ближайшие сутки по вашим темам на рабочих каналах ничего нет.</p>` : ""}`;
}

// Favourites need only their own rows: the topics block, which reads a day of the whole
// guide, fills in after the list is already on screen.
async function viewFavorites() {
  render(html`<h1>Избранное</h1>${skeleton(3)}`);
  const items = await loadFavorites();
  const channels = items.filter((f) => f.target_kind === "channel");
  const programmes = items.filter((f) => f.target_kind === "programme");
  const nowBy = new Map();
  const present = channels.filter((f) => f.name);
  if (present.length) {
    const qs = present.map((f) => `channel_id=${enc(f.target_id)}`).join("&");
    const data = await api(`/now?${qs}`);
    data.items.forEach((i) => { if (!nowBy.has(i.channel_id)) nowBy.set(i.channel_id, i); });
  }
  queueMicrotask(fillTopics);
  const topicsSlot = html`<div id="topics-slot"><h2>Темы</h2><p class="small muted">Загружаю темы…</p></div>`;
  if (!items.length) {
    return html`${heading("Избранное")}<div class="empty"><p>Здесь пока пусто.</p>
      <p class="small">Нажмите ★ у канала или передачи, чтобы добавить.</p></div>${topicsSlot}`;
  }
  return html`${heading("Избранное")}
    ${channels.length ? html`<h2>Каналы</h2><div class="grid">${channels.map((f) => {
      if (!f.name) {
        return html`<article class="card"><span class="logo" aria-hidden="true">?</span>
          <div class="body"><div class="title">Канал пропал из плейлистов</div>
          <div class="small muted">Избранное сохранено: канал вернётся, когда появится в источнике.</div></div>
          <div class="actions">${star("channel", f.target_id)}</div></article>`;
      }
      return nowCard({ logo_url: f.logo_url, ...(nowBy.get(f.target_id) || { channel_id: f.target_id, channel_name: f.name }) });
    })}</div>` : ""}
    ${programmes.length ? html`<h2>Передачи</h2><div class="grid">${programmes.map((f) => html`<article class="card">
      <div class="body"><a class="stretch title" href="#/programme/${enc(f.target_id)}">${f.name || "Передача удалена из программы"}</a></div>
      <div class="actions">${star("programme", f.target_id)}</div></article>`)}</div>` : ""}
    ${topicsSlot}`;
}

async function fillTopics() {
  const seq = renderSeq;
  try {
    const [topics, upcoming] = await Promise.all([api(`/profiles/${PROFILE}/topics`),
      api(`/profiles/${PROFILE}/alerts/upcoming?hours=24&limit=${prefs.pageSize()}`)]);
    const slot = document.getElementById("topics-slot");
    if (slot && seq === renderSeq) slot.innerHTML = String(topicsBlock(topics.items, upcoming.items, upcoming.total));
  } catch (e) {
    const slot = document.getElementById("topics-slot");
    if (slot && seq === renderSeq) slot.innerHTML = String(errorBox(e));
  }
}

async function viewSettings() {
  let health = null, metrics = null, err = null;
  try { health = await api("/health/live"); } catch (e) { err = e; }
  try { metrics = await api("/metrics/summary"); } catch (e) { err = err || e; }
  let adapters = null;
  try { adapters = await api("/playback/adapters"); } catch { /* an older server */ }
  const current = playAdapter() || adapters?.default || "agent";
  const hasToken = Boolean(store.get("mih.token"));
  return html`<h1>Настройки</h1>
    ${err ? errorBox(err) : ""}
    <form id="token-form" class="field">
      <label for="token">Токен доступа к API</label>
      <input id="token" name="token" type="password" autocomplete="off"
        placeholder="${hasToken ? "сохранён в этом браузере" : "MIH_API_TOKEN"}">
      <div class="row"><button class="btn tv" type="submit">Сохранить</button>
        ${hasToken ? html`<button class="btn" type="button" id="token-clear">Забыть токен</button>` : ""}</div>
      <span class="small muted">Хранится только в этом браузере. Без токена каталог открывается лишь на том же компьютере, где запущен сервер.</span>
    </form>
    <h2>Выдача</h2>
    <div class="field">
      <label for="pref-size">Сколько показывать сразу</label>
      <select id="pref-size">${PAGE_SIZES.map((n) => html`<option value="${n}" ${n === prefs.pageSize() ? raw("selected") : ""}>${n}</option>`)}</select>
      <span class="small muted">На страницах «Каналы» и «Поиск»; остальное по кнопке «Показать ещё».</span>
    </div>
    <div class="field">
      <label for="pref-now-size">Сколько каналов в эфире показывать сразу</label>
      <select id="pref-now-size">${PAGE_SIZES.map((n) => html`<option value="${n}" ${n === prefs.nowSize() ? raw("selected") : ""}>${n}</option>`)}</select>
      <span class="small muted">На странице «Эфир»; чем меньше, тем быстрее она открывается.</span>
    </div>
    <div class="field">
      <span>Поиск по умолчанию</span>
      <label class="row"><input type="radio" name="pref-strict" value="0" ${prefs.strict() ? "" : raw("checked")}>
        Обычный: находит формы слова («робот» найдёт «роботы») и части слов</label>
      <label class="row"><input type="radio" name="pref-strict" value="1" ${prefs.strict() ? raw("checked") : ""}>
        Строгий: только слово целиком, как написано</label>
      <span class="small muted">На странице поиска режим можно переключить для одного запроса.</span>
    </div>
    <div class="field">
      <label for="pref-relevance">Точность поиска передач</label>
      <select id="pref-relevance">${RELEVANCE.map(([v, label]) => html`<option value="${v}" ${v === prefs.relevance() ? raw("selected") : ""}>${label}</option>`)}</select>
      <span class="small muted">«Только лучшие» убирает передачи, где слово нашлось лишь в описании, если есть совпадения в названии.</span>
    </div>
    <label class="row field"><input type="checkbox" id="pref-playable" ${prefs.playableOnly() ? raw("checked") : ""}>
      Скрывать каналы и передачи, у которых нет рабочих потоков</label>
    <label class="row field"><input type="checkbox" id="logos" ${prefs.logos() ? raw("checked") : ""}>
      Показывать логотипы каналов (картинки грузятся с сайтов из плейлиста)</label>
    <p class="small muted">Эти настройки хранятся в этом браузере и действуют со следующего поиска или открытия страницы.</p>
    <h2>История поиска</h2>
    <label class="row field"><input type="checkbox" id="pref-history" ${historyOn() ? raw("checked") : ""}>
      Запоминать мои поиски и показывать их списком под строкой поиска</label>
    <div class="row field"><span class="small muted" id="history-count">Сохранено запросов: ${searchHistory().length}</span>
      <button class="btn" type="button" id="history-clear" ${searchHistory().length ? "" : raw("disabled")}>Очистить историю поиска</button></div>
    <h2>Сервер</h2>
    <div class="kv small">
      <span class="muted">Версия</span><span>${health?.version || "—"}</span>
      <span class="muted">Часовой пояс браузера</span><span>${TZ}</span>
      ${metrics ? html`<span class="muted">Каналов</span><span>${nf.format(metrics.channels)}</span>
        <span class="muted">С программой</span><span>${nf.format(metrics.channels_with_epg)}${metrics.epg_coverage != null ? ` (${Math.round(metrics.epg_coverage * 100)}%)` : ""}</span>
        <span class="muted">EPG ждёт решения</span><span>${metrics.epg_mappings_needs_review}</span>` : ""}
    </div>
    <h2>Кнопка «На ТВ»</h2>
    ${adapters ? html`<div class="field">
      <label class="row"><input type="radio" name="adapter" value="agent" ${current === "agent" ? raw("checked") : ""}>
        Через агента: он берёт команду через MCP, включает ТВ и сообщает, если не заиграло</label>
      <label class="row"><input type="radio" name="adapter" value="ha" ${current === "ha" ? raw("checked") : ""}
        ${adapters.available.includes("ha") ? "" : raw("disabled")}>
        Напрямую через Home Assistant${adapters.ha_script ? `: скрипт ${adapters.ha_script}` : " (не настроен на сервере: MIH_HA_URL, MIH_HA_TOKEN)"}</label>
      ${adapters.available.includes("ha") ? html`<div class="row"><button class="btn" type="button" id="ha-check">Проверить связь с HA</button></div>` : ""}
      <span class="small muted">Выбор хранится в этом браузере. Home Assistant получает адреса потоков и название канала и запускает свой скрипт, агент не участвует.</span>
    </div>` : ""}
    <p class="small"><a href="/api/docs">Документация API</a></p>`;
}

// ---------- Status: what the service does, did and plans ----------

const JOB_NAMES = { sources: "Обновление плейлистов", epg: "Обновление телепрограммы",
  health: "Проверка потоков", alerts: "Оповещения по темам", discover: "Поисковик плейлистов",
  pack: "Упаковщик плейлистов", drive: "Выгрузка на Google Диск", digest: "Сводка за сутки" };

function discoveryBlock(d) {
  if (!d || !d.found) return "";
  const st = d.by_status || {};
  return html`<h2>Грабер плейлистов</h2>
    <div class="kv small">
      <span class="muted">Найдено плейлистов</span><span>${d.found} (репозиториев GitHub ${d.repos}, ждут разбора ${d.repos_waiting})</span>
      <span class="muted">Ждут проверки</span><span>${st.new || 0}</span>
      <span class="muted">Живые, ждут упаковщика</span><span>${st.live || 0}</span>
      <span class="muted">Упакованы</span><span>${st.packed || 0}</span>
      <span class="muted">Мёртвые / без новых каналов / копии</span><span>${st.dead || 0} / ${st.known || 0} / ${st.duplicate || 0}</span>
      <span class="muted">Отклонены (логины, DRM, не плейлист)</span><span>${st.rejected || 0}</span>
      <span class="muted">Каналов из найденного</span><span>${d.auto_channels}, рабочих потоков ${d.auto_working} из ${d.auto_streams}</span>
    </div>`;
}
const LEVEL_ICON = { routine: "·", info: "●", warning: "▲", error: "✖" };
const JOB_STATUS = { ok: "успешно", warning: "с предупреждениями", error: "ошибка" };

function whenShort(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const day = dayLabel(d);
  return `${day === "Сегодня" ? "" : day + ", "}${time(iso)}`;
}

function everyText(min) {
  if (min % 1440 === 0) return min === 1440 ? "сутки" : `${min / 1440} дн.`;
  if (min % 60 === 0) return `${min / 60} ч`;
  return `${min} мин`;
}

function eventItem(e) {
  return html`<li class="event ${e.level}">
    <span class="lvl" aria-hidden="true">${LEVEL_ICON[e.level] || "·"}</span>
    <div><div><time class="muted">${whenShort(e.at)}</time> ${e.title}</div>
    ${e.body ? html`<div class="small muted description">${e.body}</div>` : ""}</div></li>`;
}

async function viewStatus() {
  const s = await api("/status");
  const running = s.running.length
    ? html`<ul class="plain">${s.running.map((r) => html`<li><span class="live">●</span> ${JOB_NAMES[r.job] || r.job}, с ${time(r.since)}</li>`)}</ul>`
    : html`<p class="muted">Сейчас ничего не выполняется.</p>`;
  const n = s.notifications;
  return html`${heading("Статус")}
    <div class="kv small">
      <span class="muted">Версия</span><span>${s.version}</span>
      <span class="muted">Запущен</span><span>${whenShort(s.started_at)}</span>
      <span class="muted">Планировщик</span><span>${s.scheduler ? "включён" : "выключен (MIH_SCHEDULER=0)"}</span>
      <span class="muted">Пуши</span><span>${n.channel ? `ntfy, от уровня «${n.level}»` : "не настроены (MIH_NTFY_URL)"}</span>
    </div>
    ${n.channel ? html`<p><button class="btn" type="button" id="push-test">Отправить тестовый пуш</button></p>` : ""}
    <h2>Сейчас</h2>${running}
    <h2>Задачи</h2>
    <div class="grid">${s.jobs.map((j) => html`<article class="card">
      <div class="body"><div class="title">${JOB_NAMES[j.name] || j.name}</div>
        <div class="small">${j.last_summary || "ещё не запускалась"}</div>
        <div class="small muted">${j.last_finished_at ? `Последний запуск: ${whenShort(j.last_finished_at)}, ${JOB_STATUS[j.last_status] || j.last_status}` : ""}</div>
        <div class="small muted">Следующий: ${j.running_since ? "выполняется сейчас" : (j.next_run_at ? (Date.parse(j.next_run_at) <= Date.now() ? "в ближайшую минуту" : whenShort(j.next_run_at)) : "только вручную")}</div></div>
      <div class="actions"><button class="btn" type="button" data-run-job="${j.name}" ${j.running_since ? raw("disabled") : ""}>Запустить</button></div>
    </article>`)}</div>
    ${discoveryBlock(s.discovery)}
    <h2>План на сутки</h2>
    ${s.plan.length ? html`<ul class="schedule">${s.plan.map((p) => html`<li><time>${time(p.next_at)}</time><span>${JOB_NAMES[p.job] || p.job}${p.every_minutes && p.runs.length > 4
        ? `, каждые ${everyText(p.every_minutes)}`
        : (p.runs.length > 1 ? `, затем ${p.runs.slice(1).map(time).join(", ")}` : (dayLabel(new Date(p.next_at)) === "Сегодня" ? "" : `, ${dayLabel(new Date(p.next_at)).toLowerCase()}`))}</span></li>`)}</ul>`
      : html`<p class="muted">${s.scheduler ? "На ближайшие сутки ничего не запланировано." : "Планировщик выключен: задачи запускаются только вручную."}</p>`}
    <h2>События</h2>
    ${s.events.length ? html`<ul class="events">${s.events.map(eventItem)}</ul>` : html`<div class="empty">Событий пока нет.</div>`}`;
}

// ---------- Coverage ----------

const nf = new Intl.NumberFormat("ru-RU");
const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : "—");
let regionNames = null;
try { regionNames = new Intl.DisplayNames(["ru"], { type: "region" }); } catch { /* old browser */ }
function countryName(cc) {
  if (cc === "?") return "Страна не указана";
  try { return regionNames?.of(cc) || cc; } catch { return cc; }
}
const REASONS = {
  no_stream: "Нет ссылки на поток (канал только из программы)", gone: "Поток удалён", no_answer: "Не отвечает",
  bad_manifest: "Битый плейлист потока", geo_suspect: "Похоже на гео-блок", server_error: "Ошибка сервера",
  unchecked: "Ещё не проверен", drm: "Защищён DRM", bad_content: "Не видео", bad_url: "Неверная ссылка",
  player_error: "Не играет на ТВ", absent: "Пропал из плейлиста", blocked_by_geo: "Гео-блок",
};
const DISCOVERY_STEPS = [
  ["urls_checked", "Ссылок проверено", false], ["repos", "Репозиториев найдено", false],
  ["found", "Плейлистов найдено", false], ["new", "Ждут первой пробы", false],
  ["live", "Живые, ждут упаковщика", true], ["packed", "Упакованы", true],
  ["dead", "Мёртвые", false], ["known", "Без новых каналов", false], ["duplicate", "Копии", false],
  ["rejected", "Отклонены (логины, DRM)", false],
  ["auto_streams", "Потоков упаковано", true], ["auto_channels", "Каналов из упакованного", true],
];
const SEG = ["seg-guide", "seg-work", "seg-dead", "seg-none"];

// One row of horizontal bars; segs: [[value, class, name]]; scale: the longest row's total.
function barRow(label, segs, scale, right) {
  const total = segs.reduce((a, [v]) => a + v, 0);
  const tip = `${label}: ` + segs.map(([v, , name]) => `${name} ${nf.format(v)}`).join(", ");
  return html`<div class="bar-row" title="${tip}">
    <span class="bar-label">${label}</span>
    <span class="bar-track"><span class="bar-fill" style="width:${scale ? ((100 * total) / scale).toFixed(2) : 0}%">${segs.filter(([v]) => v > 0).map(([v, cls]) =>
      html`<i class="${cls}" style="flex-grow:${v}"></i>`)}</span></span>
    <span class="bar-value">${right}</span></div>`;
}
const legend = (items) => html`<div class="legend small muted">${items.map(([cls, name]) => html`<span><i class="${cls}"></i>${name}</span>`)}</div>`;

function growthChart(history) {
  if (!history || history.length < 2) return html`<p class="small muted">График роста появится, когда наберётся хотя бы два дня наблюдений.</p>`;
  // Drawn at the panel's own width, so the labels keep their size on a phone and a desktop.
  const W = Math.max(280, Math.round(view.clientWidth - 58)), H = 200, L = 52, R = 8, T = 10, B = 24;
  const max = Math.max(...history.map((p) => p.all)) || 1;
  const x = (i) => L + ((W - L - R) * i) / (history.length - 1);
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const line = (k) => history.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[k]).toFixed(1)}`).join("");
  const ticks = [0, 0.5, 1].map((f) => Math.round(max * f));
  const series = [["all", "s-all", "все"], ["working", "s-work", "живые"], ["guide", "s-guide", "живые с программой"]];
  const fmtD = (d) => new Date(d + "T00:00:00Z").toLocaleDateString("ru-RU", { day: "numeric", month: "short", timeZone: "UTC" });
  return html`<svg class="growth" viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Рост каталога по дням">
    ${ticks.map((v) => html`<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"></line><text x="${L - 6}" y="${y(v)}" dy="0.35em" text-anchor="end">${nf.format(v)}</text>`)}
    ${series.map(([k, cls]) => html`<path class="${cls}" d="${line(k)}"></path>`)}
    <text x="${L}" y="${H - 6}">${fmtD(history[0].day)}</text>
    <text x="${W - R}" y="${H - 6}" text-anchor="end">${fmtD(history[history.length - 1].day)}</text>
  </svg>${legend(series.map(([, cls, name]) => [cls, name]))}`;
}

async function viewCoverage(params) {
  const r = await api("/report");
  if (!r.funnel) return html`<h1>Покрытие</h1><div class="empty">Отчёт ещё не построен. Он появится после ближайшей проверки потоков.</div>`;
  const f = r.funnel, d = r.discovery || {}, st = d.by_status || {};
  const tile = (label, v, note) => html`<div class="tile"><span class="small muted">${label}</span><b>${nf.format(v)}</b><span class="small muted">${note}</span></div>`;

  const funnel = [["all", "Все каналы в каталоге"], ["streams", "Есть ссылка на поток"], ["working", "Поток живой"], ["guide", "Живой и с программой"]];
  const sort = params.get("sort") === "gap" ? "gap" : "all";
  const showAll = params.get("all") === "1";
  let countries = [...(r.by_country || [])];
  if (sort === "gap") countries.sort((a, b) => (b.working - b.guide) - (a.working - a.guide));
  // Channels without a country are a pile of their own: last, and not setting the scale.
  countries = [...countries.filter((c) => c.country !== "?"), ...countries.filter((c) => c.country === "?")];
  const shown = showAll ? countries : [...countries.slice(0, 30).filter((c) => c.country !== "?"), ...countries.filter((c) => c.country === "?")];
  const cmax = Math.max(...shown.filter((c) => c.country !== "?").map((c) => c.all), 1);
  const link = (p) => "#/coverage?" + new URLSearchParams(Object.entries({ sort, all: showAll ? "1" : "", ...p }).filter(([, v]) => v));

  const reasons = Object.entries(r.not_working || {}).sort((a, b) => b[1] - a[1]);
  const rmax = Math.max(...reasons.map(([, n]) => n), 1);
  const steps = DISCOVERY_STEPS.map(([k, label, hot]) => [label, k in st ? st[k] : (d[k] ?? 0), hot]).filter(([, v]) => v);
  const dmax = Math.max(...steps.map(([, v]) => v), 1);
  const days = r.new_streams || [];
  const daymax = Math.max(...days.map((x) => x.streams), 1);
  const fmtDay = (s) => new Date(s + "T00:00:00Z").toLocaleDateString("ru-RU", { day: "numeric", month: "long", timeZone: "UTC" });
  const sources = r.sources || [];
  const smax = Math.max(...sources.map((x) => x.streams), 1);
  const g = r.guides || {}, ap = g.autopilot || {};

  return html`<h1>Покрытие</h1>
    <p class="small muted">Обновлено: ${whenShort(r.generated_at)}. Живой значит: поток отвечал при последней проверке.</p>
    <div class="tiles">
      ${tile("Все каналы", f.all, "в каталоге")}
      ${tile("Со ссылкой на поток", f.streams, pct(f.streams, f.all) + " от всех")}
      ${tile("Живые", f.working, pct(f.working, f.all) + " от всех")}
      ${tile("Живые с программой", f.guide, pct(f.guide, f.working) + " от живых")}
      ${tile("Найдено грабером", d.auto_channels || 0, "каналов")}
    </div>

    <h2>Воронка каталога</h2>
    <div class="panel bars">${funnel.map(([k, label], i) => barRow(label, [[f[k], i === 3 ? "seg-guide" : "seg-step", "каналов"]], f.all, `${nf.format(f[k])} · ${pct(f[k], f.all)}`))}</div>

    <h2>Рост каталога</h2>
    <div class="panel">${growthChart(r.history)}</div>

    <h2>По странам</h2>
    <div class="row">
      <a class="chip ${sort === "all" ? "on" : ""}" href="${link({ sort: "" })}">Больше всего каналов</a>
      <a class="chip ${sort === "gap" ? "on" : ""}" href="${link({ sort: "gap" })}">Больше всего без программы</a>
    </div>
    <div class="panel bars">
      ${legend([["seg-guide", "живой, есть программа"], ["seg-work", "живой, без программы"], ["seg-dead", "ссылка есть, не отвечает"], ["seg-none", "нет ссылки"]])}
      <p class="small muted">Справа: доля живых каналов страны, у которых есть программа.</p>
      ${shown.map((c) => barRow(countryName(c.country), [[c.guide, SEG[0], "с программой"], [c.working - c.guide, SEG[1], "без программы"],
        [Math.max(0, c.streams - c.working), SEG[2], "не отвечает"], [Math.max(0, c.all - c.streams), SEG[3], "нет ссылки"]], cmax,
        c.working ? pct(c.guide, c.working) : "—"))}
      ${countries.length > 30 ? html`<a class="btn more" href="${link({ all: showAll ? "" : "1" })}">${showAll ? "Только первые 30" : `Все страны (${countries.length})`}</a>` : ""}
    </div>

    <h2>Поисковик и упаковщик</h2>
    <div class="panel bars">${steps.map(([label, v, hot]) => barRow(label, [[v, hot ? "seg-guide" : "seg-step", label]], dmax, nf.format(v)))}</div>

    <h2>Новые потоки по дням</h2>
    <div class="panel bars">
      ${legend([["seg-guide", "живые сейчас"], ["seg-none", "не отвечают"]])}
      ${days.length ? days.map((x) => barRow(fmtDay(x.day), [[x.working, "seg-guide", "живые"], [x.streams - x.working, "seg-none", "не отвечают"]], daymax, nf.format(x.streams)))
        : html`<p class="small muted">За последние 30 дней новых потоков не было.</p>`}
    </div>

    <h2>Потоки по источникам</h2>
    <div class="panel bars">
      ${legend([["seg-guide", "живые"], ["seg-none", "не отвечают"]])}
      ${sources.map((x) => barRow(x.auto ? `Найдено грабером (${nf.format(x.playlists)} плейл.)` : x.name,
        [[x.working, "seg-guide", "живые"], [x.streams - x.working, "seg-none", "не отвечают"]], smax, nf.format(x.streams)))}
    </div>

    <h2>Почему канал не попал в живые</h2>
    <div class="panel bars">${reasons.length ? reasons.map(([k, n]) => barRow(REASONS[k] || k, [[n, k === "no_stream" ? "seg-none" : "seg-dead", "каналов"]], rmax, nf.format(n)))
      : html`<p class="small muted">Все каналы живые.</p>`}</div>

    <h2>Программа передач</h2>
    <div class="kv small">
      <span class="muted">Источников программы</span><span>${nf.format(g.sources || 0)}</span>
      <span class="muted">Автопилот: оставлены / на пробе</span><span>${ap.kept || 0} / ${ap.trial || 0}</span>
      <span class="muted">Автопилот: бесполезные / сломанные</span><span>${ap.useless || 0} / ${ap.broken || 0}</span>
    </div>`;
}

// ---------- Past searches: a list under the search field, kept in this browser ----------

const HISTORY_MAX = 30;
const historyOn = () => store.get("mih.historyOff") !== "1";

function searchHistory() {
  try {
    const v = JSON.parse(store.get("mih.history") || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string" && x) : [];
  } catch { return []; }
}

const sameQuery = (a, b) => a.toLocaleLowerCase("ru") === b.toLocaleLowerCase("ru");

function rememberSearch(q) {
  if (!q || !historyOn()) return;
  const rest = searchHistory().filter((x) => !sameQuery(x, q));
  store.set("mih.history", JSON.stringify([q, ...rest].slice(0, HISTORY_MAX)));
}

function forgetSearch(q) {
  const rest = searchHistory().filter((x) => !sameQuery(x, q));
  store.set("mih.history", rest.length ? JSON.stringify(rest) : null);
}

const searchInput = document.getElementById("search-input");
const historyBox = document.getElementById("search-history");
let historyPick = -1;

// The list shows what matches the typed text; it only fills the field and searches on a
// click or Enter, never by itself.
function showHistory() {
  const typed = searchInput.value.trim().toLocaleLowerCase("ru");
  const items = searchHistory().filter((q) => !typed || q.toLocaleLowerCase("ru").includes(typed)).slice(0, 10);
  if (!items.length || (items.length === 1 && sameQuery(items[0], searchInput.value.trim()))) { hideHistory(); return; }
  historyPick = Math.min(historyPick, items.length - 1);
  historyBox.innerHTML = String(html`${items.map((q, i) => html`<div class="item ${i === historyPick ? "on" : ""}" role="option"
      aria-selected="${i === historyPick}"><button type="button" class="q" data-history="${q}">${q}</button>
      <button type="button" class="del" data-history-del="${q}" title="Убрать из истории" aria-label="Убрать «${q}» из истории">×</button></div>`)}
    <div class="foot small"><span class="muted">Прошлые поиски</span><button type="button" data-history-clear>Очистить историю</button></div>`);
  historyBox.hidden = false;
  searchInput.setAttribute("aria-expanded", "true");
}

function hideHistory() {
  historyBox.hidden = true;
  historyPick = -1;
  searchInput.setAttribute("aria-expanded", "false");
}

function clearHistory() {
  store.set("mih.history", null);
  hideHistory();
  toast("История поиска очищена");
}

searchInput.addEventListener("focus", showHistory);
searchInput.addEventListener("input", () => { historyPick = -1; showHistory(); });
searchInput.addEventListener("blur", () => setTimeout(hideHistory, 150));
searchInput.addEventListener("keydown", (e) => {
  if (historyBox.hidden) { if (e.key === "ArrowDown") showHistory(); return; }
  const rows = historyBox.querySelectorAll(".item");
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    // -1 is the typed text itself: the arrows cycle through it and the list.
    const n = rows.length;
    if (e.key === "ArrowDown") historyPick = historyPick + 1 >= n ? -1 : historyPick + 1;
    else historyPick = historyPick <= -1 ? n - 1 : historyPick - 1;
    rows.forEach((r, i) => { r.classList.toggle("on", i === historyPick); r.setAttribute("aria-selected", String(i === historyPick)); });
  } else if (e.key === "Enter" && historyPick >= 0) {
    searchInput.value = rows[historyPick].querySelector(".q").dataset.history;
  } else if (e.key === "Escape") {
    hideHistory();
  }
});
// Clicks inside the list must not blur the field first.
historyBox.addEventListener("mousedown", (e) => e.preventDefault());
historyBox.addEventListener("click", (e) => {
  const pick = e.target.closest("[data-history]");
  const del = e.target.closest("[data-history-del]");
  if (pick) {
    searchInput.value = pick.dataset.history;
    document.getElementById("search-form").requestSubmit();
  } else if (del) {
    forgetSearch(del.dataset.historyDel);
    showHistory();
    searchInput.focus();
  } else if (e.target.closest("[data-history-clear]")) {
    clearHistory();
  }
});

// ---------- Router ----------

let renderSeq = 0;
function render(content) { view.innerHTML = String(content); }

function setTab(name) {
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === name));
}

async function route() {
  const seq = ++renderSeq;
  pagers.clear();
  const hash = location.hash.slice(1) || "/";
  const [path, query = ""] = hash.split("?");
  const params = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  let tab = "home", fn;
  if (!parts.length) fn = viewHome;
  else if (parts[0] === "search") { tab = "search"; fn = () => viewSearch(params); }
  else if (parts[0] === "channels") { tab = "channels"; fn = () => viewChannels(params); }
  else if (parts[0] === "channel" && parts[1]) { tab = "channels"; fn = () => viewChannel(parts[1], params); }
  else if (parts[0] === "programme" && parts[1]) { tab = "search"; fn = () => viewProgramme(parts[1]); }
  else if (parts[0] === "favorites") { tab = "favorites"; fn = viewFavorites; }
  else if (parts[0] === "settings") { tab = "settings"; fn = viewSettings; }
  else if (parts[0] === "status") { tab = "status"; fn = viewStatus; }
  else if (parts[0] === "coverage") { tab = "coverage"; fn = () => viewCoverage(params); }
  else fn = async () => html`<div class="empty">Страница не найдена. <a href="#/">На главную</a></div>`;
  setTab(tab);
  try {
    const out = await fn();
    if (seq === renderSeq) {
      render(out);
      // Open a channel's schedule at what is on now, not at midnight.
      if (parts[0] === "channel") view.querySelector(".schedule li.now")?.scrollIntoView({ block: "center" });
    }
  } catch (e) {
    if (seq === renderSeq) render(errorBox(e));
  }
}

function go(path, params) {
  const qs = new URLSearchParams(Object.entries(params || {}).filter(([, v]) => v));
  const next = "#" + path + (qs.toString() ? "?" + qs : "");
  if (location.hash === next) route(); else location.hash = next;
}

// ---------- Events ----------

window.addEventListener("hashchange", () => { route(); view.focus({ preventScroll: true }); window.scrollTo(0, 0); });

document.getElementById("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = document.getElementById("search-input").value.trim();
  rememberSearch(q);
  hideHistory();
  const cur = new URLSearchParams(location.hash.split("?")[1] || "");
  const keep = location.hash.startsWith("#/search") ? Object.fromEntries(SEARCH_KEYS.map((k) => [k, cur.get(k)])) : {};
  go("/search", { q, ...keep });
  document.getElementById("search-input").blur();
});

document.addEventListener("click", (e) => {
  const starBtn = e.target.closest(".star");
  if (starBtn) {
    e.preventDefault();
    toggleFav(starBtn.dataset.kind, starBtn.dataset.id).catch((err) => toast(errorText(err), 5000));
    return;
  }
  const play = e.target.closest("[data-play]");
  if (play) {
    e.preventDefault();
    playOnTv(play.dataset.play, play.dataset.feed || null, play);
    return;
  }
  const more = e.target.closest("[data-more]");
  if (more) { showMore(more); return; }
  if (e.target.closest("[data-refresh]")) {
    state.channelsLoaded = false;
    route();
    return;
  }
  const chip = e.target.closest("[data-filter]");
  if (chip) {
    // Only marks the choice (kept in the address); "Найти" or Enter runs the search.
    chip.parentElement.querySelectorAll("[data-filter]").forEach((c) => {
      c.classList.toggle("on", c === chip);
      c.setAttribute("aria-pressed", String(c === chip));
    });
    const cur = new URLSearchParams(location.hash.split("?")[1] || "");
    if (chip.dataset.value) cur.set(chip.dataset.filter, chip.dataset.value); else cur.delete(chip.dataset.filter);
    history.replaceState(null, "", "#/search?" + cur);
    return;
  }
  if (e.target.id === "search-run") {
    document.getElementById("search-form").requestSubmit();
    return;
  }
  const follow = e.target.closest("[data-follow]");
  if (follow) {
    follow.disabled = true;
    followTopic(follow.dataset.follow).catch((err) => { follow.disabled = false; toast(errorText(err), 5000); });
    return;
  }
  const delTopic = e.target.closest("[data-topic-del]");
  if (delTopic) {
    api(`/profiles/${PROFILE}/topics/${enc(delTopic.dataset.topicDel)}`, { method: "DELETE" })
      .then(() => { toast(`Тема «${delTopic.dataset.topicDel}» убрана`); route(); })
      .catch((err) => toast(errorText(err), 5000));
    return;
  }
  const runBtn = e.target.closest("[data-run-job]");
  if (runBtn) {
    runBtn.disabled = true;
    api(`/jobs/${enc(runBtn.dataset.runJob)}/run`, { method: "POST" })
      .then(() => { toast("Задача запущена. Результат появится в событиях и придёт пушем."); setTimeout(route, 1500); })
      .catch((err) => { runBtn.disabled = false; toast(errorText(err), 5000); });
    return;
  }
  if (e.target.id === "push-test") {
    api("/notifications/test", { method: "POST" })
      .then((r) => toast(r.result === "sent" ? "Тестовый пуш отправлен" : "Пуш не отправился: проверьте MIH_NTFY_URL", 5000))
      .catch((err) => toast(errorText(err), 5000));
    return;
  }
  if (e.target.id === "ha-check") {
    e.target.disabled = true;
    api("/playback/ha/check", { method: "POST" })
      .then((r) => toast(r.ok ? `Home Assistant отвечает, скрипт ${r.script} найден` : (PLAY_TEXT[r.error] || `Ошибка: ${r.error}`), 6000))
      .catch((err) => toast(errorText(err), 5000))
      .finally(() => { e.target.disabled = false; });
    return;
  }
  if (e.target.id === "history-clear") {
    clearHistory();
    e.target.disabled = true;
    document.getElementById("history-count").textContent = "Сохранено запросов: 0";
    return;
  }
  if (e.target.id === "token-clear") {
    store.set("mih.token", null);
    state.channelsLoaded = false;
    toast("Токен удалён из браузера");
    route();
  }
});

document.addEventListener("submit", (e) => {
  if (e.target.id === "token-form") {
    e.preventDefault();
    const v = e.target.token.value.trim();
    if (!v) { toast("Вставьте токен в поле"); return; }
    store.set("mih.token", v);
    state.channelsLoaded = false;
    state.channels.clear();
    // Check it at once: a wrong token should say so here, not on the next screen.
    api("/health/ready")
      .then(() => { toast("Токен сохранён"); location.hash = "#/"; })
      .catch((err) => {
        if (err instanceof ApiFailure && err.status === 401) {
          store.set("mih.token", null);
          toast("Токен не подошёл. Скопируйте MIH_API_TOKEN целиком, без пробелов", 6000);
        } else toast(errorText(err), 5000);
        route();
      });
  } else if (e.target.id === "channel-filter") {
    e.preventDefault();
    const f = new FormData(e.target);
    go("/channels", { q: (f.get("q") || "").trim(), country: f.get("country"), epg: f.get("epg") });
  } else if (e.target.id === "topic-form") {
    e.preventDefault();
    const topic = e.target.topic.value.trim();
    if (topic) followTopic(topic).then(route).catch((err) => toast(errorText(err), 5000));
  }
});

// Settings save as they change; nothing on screen reloads by itself.
const PREF_KEYS = { "pref-size": "mih.pageSize", "pref-now-size": "mih.nowSize", "pref-relevance": "mih.relevance" };
document.addEventListener("change", (e) => {
  const t = e.target;
  if (PREF_KEYS[t.id]) { store.set(PREF_KEYS[t.id], t.value); toast("Сохранено"); }
  else if (t.name === "pref-strict") { store.set("mih.strict", t.value); toast("Сохранено"); }
  else if (t.id === "pref-playable") { store.set("mih.playable", t.checked ? "1" : "0"); toast("Сохранено"); }
  else if (t.id === "pref-history") {
    store.set("mih.historyOff", t.checked ? null : "1");
    toast(t.checked ? "Поиски будут запоминаться" : "Новые поиски больше не запоминаются");
  }
});

document.addEventListener("input", (e) => {
  if (e.target.name === "adapter") {
    store.set("mih.adapter", e.target.value);
    toast(e.target.value === "ha" ? "«На ТВ» теперь идёт напрямую в Home Assistant" : "«На ТВ» теперь выполняет агент");
  }
  if (e.target.id === "logos") {
    store.set("mih.logos", e.target.checked ? "1" : "0");
    toast("Сохранено");
  }
});

// A logo that fails to load turns into initials.
document.addEventListener("error", (e) => {
  const img = e.target;
  if (img.tagName === "IMG" && img.dataset.fallback !== undefined) {
    const span = document.createElement("span");
    span.className = img.className;
    span.textContent = img.dataset.fallback;
    span.setAttribute("aria-hidden", "true");
    img.replaceWith(span);
  }
}, true);

// No timers re-render a screen: it changes only on a click (↻ Обновить, a tab, a link).

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => { /* http on a LAN address: no PWA, still works */ });
}

route();
