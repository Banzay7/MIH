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
const RELEVANCE = [["0", t("Все совпадения")], ["0.3", t("Хорошие совпадения")], ["0.7", t("Только лучшие")]];
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

// ---------- Answers kept in the browser ----------
// A read (and a search) is answered from the browser's copy while the server's data
// version is the same: it moves only when playlists, the guide or stream checks change.
// Anything with programme times is also kept only until the next of them starts or ends.
// «↻ Обновить» always asks the server.

const KEEP_PREFIX = "mih.c:";
const KEEP_CHARS = 400000; // bigger answers stay in memory only
const KEEP_MIN = 30e3, KEEP_MAX = 6 * 3600e3;
const LIVE_PATHS = /^\/(status|health|playback|data-version|jobs|notifications)/;
const kept = new Map();
let dataVersion = store.get("mih.dataVersion") || "";
let fresh = false;

function keepUntil(data) {
  const now = Date.now();
  let next = now + KEEP_MAX;
  const walk = (o) => {
    if (Array.isArray(o)) { o.forEach(walk); return; }
    if (!o || typeof o !== "object") return;
    for (const [k, v] of Object.entries(o)) {
      if ((k === "start" || k === "stop") && typeof v === "string") {
        const at = Date.parse(v);
        if (at > now && at < next) next = at;
      } else if (v && typeof v === "object") walk(v);
    }
  };
  walk(data);
  return Math.max(next, now + KEEP_MIN);
}

function keptGet(key) {
  let e = kept.get(key);
  if (!e) { try { e = JSON.parse(localStorage.getItem(KEEP_PREFIX + key)); } catch { e = null; } }
  if (!e || e.v !== dataVersion || Date.now() >= e.until) return null;
  kept.set(key, e);
  return e.data;
}

function keptSet(key, data) {
  const e = { v: dataVersion, until: keepUntil(data), data };
  kept.set(key, e);
  try {
    const s = JSON.stringify(e);
    if (s.length < KEEP_CHARS) localStorage.setItem(KEEP_PREFIX + key, s);
  } catch { forgetKept(); } // storage full: start over rather than keep half
}

function forgetKept() {
  kept.clear();
  state.channelsLoaded = false;
  try {
    Object.keys(localStorage).filter((k) => k.startsWith(KEEP_PREFIX)).forEach((k) => localStorage.removeItem(k));
  } catch { /* private mode */ }
}

function noteVersion(v) {
  if (!v || v === dataVersion) return;
  if (dataVersion) forgetKept();
  dataVersion = v;
  store.set("mih.dataVersion", v);
}

// One tiny request per screen: is what the browser keeps still current?
async function checkVersion() {
  try {
    const r = await fetch(API + "/data-version", { cache: "no-store" });
    if (r.ok) noteVersion((await r.json()).data);
  } catch { /* offline: the request itself will say so */ }
}

async function api(path, { method = "GET", body } = {}) {
  const keepable = (method === "GET" && !LIVE_PATHS.test(path)) || path === "/search";
  const key = keepable ? method + " " + path + (body === undefined ? "" : " " + JSON.stringify(body)) : null;
  if (key && !fresh) {
    const hit = keptGet(key);
    if (hit) return hit;
  }
  const headers = { Accept: "application/json" };
  const token = store.get("mih.token");
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(API + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  noteVersion(resp.headers.get("X-MIH-Data"));
  // Own change (favourites, topics, sources): what is kept may show the old state.
  if (method !== "GET" && !key && !LIVE_PATHS.test(path)) forgetKept();
  if (resp.status === 204) return null;
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  if (!resp.ok) {
    const err = data?.error;
    throw new ApiFailure(resp.status, err?.code || (resp.status === 401 ? "unauthorized" : "http"),
      err?.message || data?.detail || resp.statusText);
  }
  if (key) keptSet(key, data);
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
    b.title = !on ? t("Убрать из избранного") : t("В избранное");
  });
  toast(on ? t("Убрано из избранного") : t("Добавлено в избранное"));
}

// ---------- Playback ("На ТВ") ----------

const PLAY_TEXT = {
  accepted: t("Передаю команду агенту…"),
  taken: t("Агент включает канал на ТВ"),
  all_failed: t("Ни один поток этого канала не заиграл на ТВ."),
  started: t("Запущено на ТВ"),
  unconfirmed: t("Канал грузится дольше обычного: ТВ ещё не показал картинку. Обычно она появляется через несколько секунд."),
  no_adapter: t("Поток выбран, но запуск на ТВ выключен на сервере (MIH_PLAYBACK=off)."),
  no_agent: t("Агент не взял команду: он не подключён к MCP или не проверяет очередь."),
  no_stream: t("У этого канала сейчас нет рабочего потока."),
  ha_started: t("Home Assistant выполнил скрипт: канал включается на ТВ"),
  ha_unreachable: t("Home Assistant недоступен с сервера: проверьте MIH_HA_URL и связь (Tailscale)."),
  ha_unauthorized: t("Home Assistant не принял токен: создайте новый и пропишите в MIH_HA_TOKEN."),
  ha_script_not_found: t("В Home Assistant нет такого скрипта: проверьте MIH_HA_SCRIPT."),
  ha_script_failed: t("Скрипт Home Assistant завершился с ошибкой. Подробности в журнале HA."),
  ha_timeout: t("Home Assistant не ответил вовремя. Канал, возможно, всё же включится."),
  ha_not_playing: t("Скрипт Home Assistant сообщил, что канал не включился"),
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
    if (store.get("mih.quality")) body.quality = store.get("mih.quality");
    let cmd = await api("/playback/commands", { method: "POST", body });
    // The agent has 30 s to take the command; then silence for 100 s means it plays,
    // so once it is taken the toast says so and only a failure or a slow start changes it.
    let told = false;
    for (let i = 0; cmd.state === "accepted" && i < 65; i++) {
      if (!told) toast(cmd.taken ? PLAY_TEXT.taken : PLAY_TEXT.accepted, cmd.taken ? 6000 : 3000);
      if (cmd.taken) { told = true; if (button) button.disabled = false; }
      await new Promise((r) => setTimeout(r, 2000));
      cmd = await api(`/playback/commands/${enc(cmd.id)}`);
    }
    if (cmd.state === "started" && cmd.detail === "unconfirmed") toast(PLAY_TEXT.unconfirmed, 8000);
    else if (told && cmd.state === "started") return;
    else if (cmd.state === "started") toast(cmd.adapter === "ha" ? PLAY_TEXT.ha_started : PLAY_TEXT.started);
    else if (cmd.state === "failed") {
      const text = PLAY_TEXT[cmd.error] || t("Не удалось запустить: {0}", cmd.error);
      toast(cmd.detail ? `${text}: ${cmd.detail}` : text, 8000);
    }
    else toast(t("ТВ не ответил вовремя. Попробуйте ещё раз."));
  } catch (e) {
    toast(errorText(e), 6000);
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------- Formatting ----------

const fmtTime = new Intl.DateTimeFormat(LOCALE, { hour: "2-digit", minute: "2-digit" });
const fmtDay = new Intl.DateTimeFormat(LOCALE, { weekday: "short", day: "numeric", month: "short" });
const time = (iso) => fmtTime.format(new Date(iso));

function dayLabel(d) {
  const today = startOfDay(new Date());
  const diff = Math.round((startOfDay(d) - today) / 86400000);
  if (diff === 0) return t("Сегодня");
  if (diff === 1) return t("Завтра");
  if (diff === -1) return t("Вчера");
  return fmtDay.format(d);
}

function startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }

function whenText(a) {
  const s = new Date(a.start), now = Date.now();
  const range = `${time(a.start)}–${time(a.stop)}`;
  if (s <= now && new Date(a.stop) > now) return html`<span class="live">${t("● В эфире")}</span> ${range}`;
  const day = dayLabel(s);
  return `${day === t("Сегодня") ? "" : day + ", "}${range}`;
}

function progress(a) {
  const s = Date.parse(a.start), e = Date.parse(a.stop), now = Date.now();
  if (!(s <= now && now < e)) return "";
  const pct = Math.max(0, Math.min(100, ((now - s) / (e - s)) * 100));
  return html`<div class="progress" aria-hidden="true"><i style="width:${pct.toFixed(1)}%"></i></div>`;
}

const LANGS = { ru: t("русский"), en: t("английский"), vi: t("вьетнамский") };
// Any language code ("ja", "rus", "vi-VN") by its name in the interface language.
let langNames = null;
try { langNames = new Intl.DisplayNames([LANG], { type: "language" }); } catch { /* old browser */ }
function langName(code) {
  try { const n = langNames?.of(code); if (n && n.toLowerCase() !== String(code).toLowerCase()) return n; } catch { /* odd code */ }
  return LANGS[code] || code;
}
const FIELDS = { title: t("в названии"), sub_title: t("в подзаголовке"), description: t("в описании"),
  categories: t("в жанре"), channel_name: t("в названии канала"), time: t("время"), language: t("язык"),
  topic: t("тема"), programme: t("в избранном") };
const TIMES = { now: t("сейчас"), today: t("сегодня"), tomorrow: t("завтра"), tonight: t("вечером"),
  yesterday: t("вчера"), custom: t("свой интервал") };

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
    aria-pressed="${on}" title="${on ? t("Убрать из избранного") : t("В избранное")}"
    aria-label="${t("Избранное")}">★</button>`;
}

function tvButton(channelId, feedId) {
  return html`<button class="btn tv" data-play="${channelId}" data-feed="${feedId || ""}">${t("На ТВ")}</button>`;
}

function errorText(e) {
  if (e instanceof ApiFailure) {
    if (e.status === 401) return t("Нужен токен доступа: укажите его в настройках.");
    if (e.code === "not_found") return t("Не найдено.");
    return t("Ошибка сервера: {0}", e.message);
  }
  return t("Сервер недоступен. Проверьте соединение.");
}

function errorBox(e) {
  const needToken = e instanceof ApiFailure && e.status === 401;
  return html`<div class="error"><p>${errorText(e)}</p>
    ${needToken ? html`<p><a class="btn" href="#/settings">${t("Открыть настройки")}</a></p>` : ""}</div>`;
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
  pagers.set(id, { items: [...items], card, total, fetchMore, size, shown: first.length });
  return html`<div class="grid" id="${id}">${first.map(card)}</div>${moreButton(id)}`;
}

function moreButton(id) {
  const p = pagers.get(id);
  const left = p.total - p.shown;
  if (left <= 0) return "";
  const next = Math.min(left, p.size);
  return html`<button class="btn more" type="button" data-more="${id}">${t("Показать ещё {0} · осталось {1}", next, nf.format(left))}</button>`;
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

const refreshButton = () => html`<button class="btn" type="button" data-refresh title="${t("Загрузить свежие данные")}">${t("↻ Обновить")}</button>`;
const heading = (title) => html`<div class="head"><h1>${title}</h1>${refreshButton()}</div>`;

// ---------- Cards ----------

function nowCard(item) {
  const name = item.channel_name || state.channels.get(item.channel_id)?.canonical_name || t("Канал");
  const cur = item.now, nxt = item.next;
  return html`<article class="card">
    ${logo(item.channel_id, name, false, item.logo_url)}
    <div class="body">
      <a class="stretch title" href="#/channel/${enc(item.channel_id)}">${name}</a>
      ${cur ? html`<div class="line">${cur.title}</div>${progress(cur)}
        <div class="small muted">${time(cur.start)}–${time(cur.stop)}</div>`
        : html`<div class="muted small">${t("Сейчас нет данных о программе")}</div>`}
      ${nxt ? html`<div class="small muted line">${t("Далее {0} · {1}", time(nxt.start), nxt.title)}</div>` : ""}
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
        ${why.map((w) => html`<span class="chip" title="${t("Почему найдено")}">${FIELDS[w.field] || w.field}: ${w.matched && w.matched !== "substring" ? w.matched : w.term}</span>`)}
        ${a.lang ? html`<span class="chip">${langName(a.lang)}</span>` : ""}
        ${a.more_airings ? html`<span class="chip">${t("ещё показов: {0}", a.more_airings)}</span>` : ""}
        ${a.streams_available === 0 ? html`<span class="chip">${t("нет рабочих потоков")}</span>` : ""}
      </div>
    </div>
    <div class="actions">${star("programme", a.programme_id)}${tvButton(a.channel_id, a.feed_id)}</div>
  </article>`;
}

function channelCard(c) {
  const id = c.id || c.channel_id;
  const name = c.canonical_name || c.channel_name;
  const streams = c.streams ?? c.streams_available;
  const facts = [c.countries?.length ? c.countries.join(", ") : c.country, c.has_epg === false ? t("без программы") : c.has_epg ? t("есть программа") : null,
    streams === 0 ? t("нет рабочих потоков") : null].filter(Boolean);
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
  render(html`<h1>${t("Сейчас в эфире")}</h1>${skeleton()}`);
  const size = prefs.nowSize();
  const playable = prefs.playableOnly() ? "&playable_only=true" : "";
  const [, data, soon] = await Promise.all([loadFavorites(),
    api(`/now?on_air=true&limit=${size}${playable}`), api(`/now?soon=45&limit=12${playable}`)]);
  if (!data.channels) {
    return html`<h1>${t("Сейчас в эфире")}</h1>
      <div class="empty"><p>${t("Каталог пока пуст.")}</p>
      <p class="small">${t("Добавьте свой плейлист и программу передач (EPG): командой")}
      <code>mih import-file</code> ${t("или через API, см.")} <a href="/api/docs">${t("документацию API")}</a>.</p></div>`;
  }
  const favIds = [...state.favorites].filter((k) => k.startsWith("channel:")).map((k) => k.slice(8));
  const favNow = favIds.length
    ? (await api(`/now?on_air=true&${favIds.map((id) => `channel_id=${enc(id)}`).join("&")}`)).items : [];
  const noGuide = data.channels - data.guided;
  const more = async (offset, limit) => (await api(`/now?on_air=true&limit=${limit}&offset=${offset}${playable}`)).items;
  return html`
    ${heading(t("Сейчас в эфире"))}
    <p class="muted small">${t("{0} в эфире · {1} каналов в каталоге{2}", nf.format(data.total), nf.format(data.channels), noGuide > 0 ? t(" · без программы: {0}", nf.format(noGuide)) : "")}</p>
    ${favNow.length ? html`<h2>${t("Избранное")}</h2><div class="grid">${favNow.map(nowCard)}</div>` : ""}
    ${soon.items.length ? html`<h2>${t("Скоро начнётся")}</h2><div class="grid">${soon.items.map((i) => airingCard({
      ...i.next, channel_name: i.channel_name, logo_url: i.logo_url }))}</div>` : ""}
    <h2>${t("Все каналы в эфире{0}", data.total ? ` · ${nf.format(data.total)}` : "")}</h2>
    ${data.total ? paged(data.items, nowCard, { total: data.total, fetchMore: more, size })
      : html`<div class="empty">${t("Нет данных о текущем эфире. Загрузите программу передач (EPG).")}</div>`}`;
}

const SEARCH_TIMES = [["", t("Любое время")], ["now", t("Сейчас")], ["today", t("Сегодня")], ["tonight", t("Вечером")], ["tomorrow", t("Завтра")]];
// Languages of the guide and the catalog (GET /search/languages): Russian, Vietnamese and
// English first, then the rest by name in the interface language.
const LANGS_FIRST = ["ru", "vi", "en"];
async function searchLangOptions(current) {
  let codes = [];
  try { codes = (await api("/search/languages")).items.map((l) => l.code); } catch { /* list stays short */ }
  codes = [...new Set([...LANGS_FIRST, ...codes, ...(current ? [current] : [])])];
  const upper = (s) => s.charAt(0).toLocaleUpperCase(LOCALE) + s.slice(1);
  const first = LANGS_FIRST.map((c) => [c, upper(langName(c))]);
  const rest = codes.filter((c) => !LANGS_FIRST.includes(c)).map((c) => [c, upper(langName(c))])
    .sort((a, b) => a[1].localeCompare(b[1], LOCALE));
  return [["", t("Любой язык")], ...first, ...rest];
}
const SEARCH_TYPES = [["", t("Всё")], ["airing", t("Передачи")], ["channel", t("Каналы")]];
const SEARCH_MODES = [["0", t("Обычный поиск")], ["1", t("Строгий: целые слова")]];
const SEARCH_KEYS = ["time", "lang", "type", "strict", "genre"];
// The server's genres (mih/search/genres.py), in the order the list shows them.
const GENRES = [["news", t("Новости")], ["sport", t("Спорт")], ["movie", t("Фильмы")], ["series", t("Сериалы")],
  ["cartoon", t("Мультфильмы")], ["kids", t("Детям")], ["romance", t("Мелодрамы и романтика")], ["comedy", t("Комедии")],
  ["drama", t("Драмы")], ["action", t("Боевики и приключения")], ["crime", t("Детективы и триллеры")], ["horror", t("Ужасы")],
  ["scifi", t("Фантастика и фэнтези")], ["documentary", t("Документальные")], ["music", t("Музыка")],
  ["entertainment", t("Развлекательные")], ["cooking", t("Кулинария")], ["travel", t("Путешествия")],
  ["nature", t("Природа и животные")], ["science", t("Наука и техника")], ["history", t("История")],
  ["health", t("Здоровье")], ["religion", t("Религия")], ["adult", t("Для взрослых")]];
const genreName = (id) => GENRES.find(([g]) => g === id)?.[1] || id;

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
  const genre = GENRES.some(([g]) => g === params.get("genre")) ? params.get("genre") : "";
  document.getElementById("search-input").value = q;
  const langOptions = await searchLangOptions(lang);
  const filters = html`<div class="filters">
    ${chipRow("time", SEARCH_TIMES, timeF)}${chipRow("type", SEARCH_TYPES, type)}
    <div class="row"><select data-filter-select="lang" aria-label="${t("Язык")}">
      ${langOptions.map(([c, label]) => html`<option value="${c}" ${c === lang ? raw("selected") : ""}>${label}</option>`)}
    </select>
    <select data-filter-select="genre" aria-label="${t("Тематика")}">
      <option value="">${t("Любая тематика")}</option>
      ${GENRES.map(([g, label]) => html`<option value="${g}" ${g === genre ? raw("selected") : ""}>${label}</option>`)}
    </select></div>
    ${chipRow("strict", SEARCH_MODES, strict)}
    <div class="row"><button class="btn tv" type="button" id="search-run">${t("Найти")}</button>
      <span class="small muted">${t("или Enter в строке поиска")}</span></div>
  </div>`;
  // Opening the tab does not search; «Найти» or Enter does, even with the field empty:
  // then only the filters choose, and with none it lists what is on now and next.
  if (!q && !timeF && !genre && params.get("run") !== "1") {
    return html`<h1>${t("Поиск")}</h1>${filters}
      <div class="empty"><p>${t("Ищите передачи и каналы обычными словами.")}</p>
      <p class="small">${t("Можно не писать ничего, а выбрать тематику и нажать «Найти». Пустой поиск без фильтров покажет всё, что идёт сейчас и в ближайшие часы.")}</p>
      <p class="small">${t("Например: «новости вечером», «футбол завтра», «роботы на русском», «Первый канал».")}</p></div>`;
  }
  render(html`<h1>${t("Поиск")}</h1>${filters}${skeleton(3)}`);
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const body = { query: q, tz: TZ, limit: prefs.pageSize(), filters: {}, strict: strict === "1",
    min_relevance: Number(prefs.relevance()), playable_only: prefs.playableOnly() };
  if (timeF) body.filters.time = timeF;
  if (lang) body.filters.languages = [lang];
  if (type) body.target_types = [type];
  if (genre) body.filters.genres = [genre];
  const res = await api("/search", { method: "POST", body });
  const total = res.total || {};
  const more = (kind) => async (offset, limit) =>
    (await api("/search", { method: "POST", body: { ...body, target_types: [kind], offset, limit } }))
      .items.filter((i) => i.type === kind);
  const pq = res.query;
  const understood = [
    pq.terms.length ? t("слова: {0}", pq.terms.join(", ")) : null,
    pq.time ? t("время: {0}", TIMES[pq.time] || pq.time) : null,
    pq.languages.length ? t("язык: {0}", pq.languages.map(langName).join(", ")) : null,
    genre ? t("тематика: {0}", genreName(genre)) : null,
  ].filter(Boolean);
  const channels = res.items.filter((i) => i.type === "channel");
  const airings = res.items.filter((i) => i.type === "airing");
  return html`<h1>${t("Поиск")}</h1>${filters}
    ${understood.length ? html`<p class="small muted">${t("Понял запрос так: {0}", understood.join(" · "))}</p>` : ""}
    ${pq.terms.length ? html`<p><button class="btn" type="button" data-follow="${pq.terms.join(" ")}">${t("🔔 Следить за темой «{0}»", pq.terms.join(" "))}</button></p>` : ""}
    ${channels.length ? html`<h2>${t("Каналы · {0}", nf.format(total.channel ?? channels.length))}</h2>
      ${paged(channels, channelCard, { total: total.channel ?? channels.length, fetchMore: more("channel") })}` : ""}
    ${airings.length ? html`<h2>${t("Передачи · {0}", nf.format(total.airing ?? airings.length))}</h2>
      ${paged(airings, airingCard, { total: total.airing ?? airings.length, fetchMore: more("airing") })}` : ""}
    ${!res.items.length ? html`<div class="empty">${t("Ничего не нашлось. Попробуйте другие слова или уберите фильтры.")}</div>` : ""}`;
}

// Russia and Vietnam first (the beta's audiences), then «all», then every other country
// present, by its name in the interface language.
const COUNTRIES_FIRST = ["RU", "VN"];

function countryOptions(codes) {
  const counts = new Map();
  codes.forEach((c) => counts.set(c, (counts.get(c) || 0) + 1));
  const label = (c) => `${countryName(c)} (${nf.format(counts.get(c))})`;
  const first = COUNTRIES_FIRST.filter((c) => counts.has(c)).map((c) => [c, label(c)]);
  const rest = [...counts.keys()].filter((c) => !COUNTRIES_FIRST.includes(c))
    .map((c) => [c, label(c)]).sort((a, b) => a[1].localeCompare(b[1], LOCALE));
  return counts.size ? [...first, ["", t("Все страны")], ...rest] : [];
}

async function viewChannels(params) {
  const q = params.get("q") || "";
  const epg = params.get("epg") === "1";
  const country = params.get("country") || "";
  const playable = prefs.playableOnly();
  render(html`<h1>${t("Каналы")}</h1>${skeleton(6)}`);
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const all = [...state.channels.values()];
  const countriesOf = (c) => c.countries || (c.country ? [c.country] : []);
  const countries = countryOptions(all.flatMap(countriesOf));
  const needle = q.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
  const list = all.filter((c) => (!epg || c.has_epg) && (!country || countriesOf(c).includes(country)) &&
    (!playable || c.streams !== 0) &&
    (!needle || c.canonical_name.toLocaleLowerCase("ru").replaceAll("ё", "е").includes(needle)));
  return html`<h1>${t("Каналы")}</h1>
    <form class="filters row" id="channel-filter">
      <div class="combo"><input id="channel-q" name="q" type="search" value="${q}" autocomplete="off"
        placeholder="${t("Название канала")}" aria-label="${t("Фильтр по названию")}"
        role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="channel-history">
        <div id="channel-history" class="history" role="listbox" aria-label="${t("Прошлые фильтры каналов")}" hidden></div></div>
      ${countries.length ? html`<select name="country" aria-label="${t("Страна")}">
        ${countries.map(([c, label]) => html`<option value="${c}" ${c === country ? raw("selected") : ""}>${label}</option>`)}
      </select>` : ""}
      <label class="row small"><input type="checkbox" name="epg" value="1" ${epg ? raw("checked") : ""}> ${t("только с программой")}</label>
      <div class="row"><button class="btn tv" type="submit">${t("Показать")}</button></div>
    </form>
    <p class="small muted">${t("Найдено: {0}{1}", nf.format(list.length), playable ? t(" · без рабочих потоков скрыты (см. настройки)") : "")}</p>
    ${list.length ? paged(list, channelCard) : html`<div class="empty">${t("Нет каналов под эти условия.")}</div>`}`;
}

function feedLabel(f) {
  const parts = [];
  if (f.region) parts.push(f.region);
  if (f.offset_minutes) parts.push(t("{0}{1} ч", f.offset_minutes > 0 ? "+" : "", f.offset_minutes / 60));
  if (f.audio_lang) parts.push(langName(f.audio_lang));
  return parts.join(", ") || t("Основной");
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
        <div class="muted small">${[ch.country, t("потоков: {0}", playable)].filter(Boolean).join(" · ")}</div>
      </div>
      <div class="row">${star("channel", id)}${tvButton(id, feedId)}</div>
    </div>
    ${feeds.length > 1 ? html`<div class="row" role="group" aria-label="${t("Версия канала")}">${feeds.map((f) =>
      html`<a class="chip ${f.id === feedId ? "on" : ""}" href="${link({ feed: f.id })}">${feedLabel(f)}</a>`)}</div>` : ""}
    <h2>${t("Программа")}</h2>
    ${feed?.epg.available ? html`
      <div class="row" role="group" aria-label="${t("День")}">${days.map(([d, label]) =>
        html`<a class="chip ${d === dayOffset ? "on" : ""}" href="${link({ day: String(d) })}">${label}</a>`)}</div>
      <p></p>
      ${schedule.length ? html`<ol class="schedule">${schedule.map((a) => {
        const s = Date.parse(a.start), e = Date.parse(a.stop);
        const cls = s <= now && now < e ? "now" : e <= now ? "past" : "";
        return html`<li class="${cls}"><time datetime="${a.start}">${time(a.start)}</time>
          <a href="#/programme/${enc(a.programme_id)}"><div>${a.title}${cls === "now" ? html` <span class="live small">${t("● в эфире")}</span>` : ""}</div>
          ${a.sub_title ? html`<div class="small muted">${a.sub_title}</div>` : ""}
          ${cls === "now" ? progress(a) : ""}</a></li>`;
      })}</ol>` : html`<div class="empty">${t("На этот день программы нет.")}</div>`}`
      : html`<div class="empty">${t("Для этого канала нет программы передач. Её можно привязать в очереди EPG.")}</div>`}
    ${ch.aliases.length ? html`<h2>${t("Другие названия")}</h2><div class="chips">${[...new Set(ch.aliases.map((a) => a.display).filter(Boolean))]
      .map((n) => html`<span class="chip">${n}</span>`)}</div>` : ""}`;
}

async function viewProgramme(id) {
  render(skeleton(2));
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const p = await api(`/programmes/${enc(id)}`);
  const now = Date.now();
  const upcoming = p.airings.filter((a) => Date.parse(a.stop) > now);
  const meta = [p.episode_num, p.lang ? langName(p.lang) : null, p.rating].filter(Boolean);
  const other = p.titles.filter((x) => x.text !== p.title);
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
    ${other.length ? html`<p class="small muted">${t("Также: {0}", other.map((x) => x.text).join(" / "))}</p>` : ""}
    <h2>${t("Когда покажут")}</h2>
    ${upcoming.length ? html`<div class="grid">${upcoming.map((a) => {
      const name = state.channels.get(a.channel_id)?.canonical_name || "";
      return html`<article class="card">${logo(a.channel_id, name)}
        <div class="body"><a class="stretch title" href="#/channel/${enc(a.channel_id)}">${name}</a>
        <div class="small muted">${whenText(a)}${a.timeshift_minutes ? t(" · +{0} ч", a.timeshift_minutes / 60) : ""}</div>${progress(a)}</div>
        <div class="actions">${tvButton(a.channel_id, a.feed_id)}</div></article>`;
    })}</div>` : html`<div class="empty">${t("Ближайших показов нет.")}</div>`}`;
}

// ---------- Topics: pushes before a programme on a followed topic starts ----------

async function followTopic(topic) {
  const r = await api(`/profiles/${PROFILE}/topics`, { method: "POST", body: { topic } });
  toast(t("Тема «{0}» добавлена: пуш придёт перед началом передачи", r.topic), 4000);
}

function alertCard(m) {
  const ch = m.channels[0];
  return airingCard({
    programme_id: m.programme_id, title: m.title, sub_title: m.sub_title, start: m.start,
    stop: m.stop, channel_id: ch.channel_id,
    channel_name: m.channels.length > 1 ? t("{0} и ещё {1}", ch.name, m.channels.length - 1) : ch.name,
    why_matched: m.reasons.map((r) => ({ field: r.kind, term: r.kind === "topic" ? r.value : t("передача") })),
  });
}

function topicsBlock(topics, upcoming, total = upcoming.length) {
  return html`<h2>${t("Темы")}</h2>
    <p class="small muted">${t("Перед началом передачи по теме придёт пуш со ссылкой на запуск. Тема ищется в названии, подзаголовке и жанре передачи, с начала слова.")}</p>
    ${topics.length ? html`<div class="chips">${topics.map((tp) => html`<button type="button" class="chip on" data-topic-del="${tp.topic}"
      title="${t("Убрать тему")}" aria-label="${t("Убрать тему {0}", tp.topic)}">${tp.topic} ×</button>`)}</div>` : ""}
    <form id="topic-form" class="row field">
      <input name="topic" minlength="2" maxlength="64" required autocomplete="off" aria-label="${t("Новая тема")}"
        placeholder="${t("Например: футбол, формула 1, роботы")}">
      <button class="btn tv" type="submit">${t("Следить")}</button>
    </form>
    ${upcoming.length ? html`<h2>${t("Скоро по вашим темам{0}", total > upcoming.length ? t(" · первые {0} из {1}", upcoming.length, nf.format(total)) : "")}</h2><div class="grid">${upcoming.map(alertCard)}</div>`
      : topics.length ? html`<p class="muted small">${t("В ближайшие сутки по вашим темам на рабочих каналах ничего нет.")}</p>` : ""}`;
}

// Favourites need only their own rows: the topics block, which reads a day of the whole
// guide, fills in after the list is already on screen.
async function viewFavorites() {
  render(html`<h1>${t("Избранное")}</h1>${skeleton(3)}`);
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
  const topicsSlot = html`<div id="topics-slot"><h2>${t("Темы")}</h2><p class="small muted">${t("Загружаю темы…")}</p></div>`;
  if (!items.length) {
    return html`${heading(t("Избранное"))}<div class="empty"><p>${t("Здесь пока пусто.")}</p>
      <p class="small">${t("Нажмите ★ у канала или передачи, чтобы добавить.")}</p></div>${topicsSlot}`;
  }
  return html`${heading(t("Избранное"))}
    ${channels.length ? html`<h2>${t("Каналы")}</h2><div class="grid">${channels.map((f) => {
      if (!f.name) {
        return html`<article class="card"><span class="logo" aria-hidden="true">?</span>
          <div class="body"><div class="title">${t("Канал пропал из плейлистов")}</div>
          <div class="small muted">${t("Избранное сохранено: канал вернётся, когда появится в источнике.")}</div></div>
          <div class="actions">${star("channel", f.target_id)}</div></article>`;
      }
      return nowCard({ logo_url: f.logo_url, ...(nowBy.get(f.target_id) || { channel_id: f.target_id, channel_name: f.name }) });
    })}</div>` : ""}
    ${programmes.length ? html`<h2>${t("Передачи")}</h2><div class="grid">${programmes.map((f) => html`<article class="card">
      <div class="body"><a class="stretch title" href="#/programme/${enc(f.target_id)}">${f.name || t("Передача удалена из программы")}</a></div>
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
  return html`<h1>${t("Настройки")}</h1>
    ${err ? errorBox(err) : ""}
    <div class="field">
      <label for="pref-lang">${t("Язык интерфейса")}</label>
      <select id="pref-lang">${Object.entries(LANGS_UI).map(([code, name]) => html`<option value="${code}" ${code === LANG ? raw("selected") : ""}>${name}</option>`)}</select>
      <span class="small muted">${t("Слова времени и языка («сегодня», «вечером», «на русском») поиск понимает по-русски и по-английски; названия ищутся на любом языке.")}</span>
    </div>
    <form id="token-form" class="field">
      <label for="token">${t("Токен доступа к API")}</label>
      <input id="token" name="token" type="password" autocomplete="off"
        placeholder="${hasToken ? t("сохранён в этом браузере") : "MIH_API_TOKEN"}">
      <div class="row"><button class="btn tv" type="submit">${t("Сохранить")}</button>
        ${hasToken ? html`<button class="btn" type="button" id="token-clear">${t("Забыть токен")}</button>` : ""}</div>
      <span class="small muted">${t("Хранится только в этом браузере. Без токена каталог открывается лишь на том же компьютере, где запущен сервер.")}</span>
    </form>
    <h2>${t("Выдача")}</h2>
    <div class="field">
      <label for="pref-size">${t("Сколько показывать сразу")}</label>
      <select id="pref-size">${PAGE_SIZES.map((n) => html`<option value="${n}" ${n === prefs.pageSize() ? raw("selected") : ""}>${n}</option>`)}</select>
      <span class="small muted">${t("На страницах «Каналы» и «Поиск»; остальное по кнопке «Показать ещё».")}</span>
    </div>
    <div class="field">
      <label for="pref-now-size">${t("Сколько каналов в эфире показывать сразу")}</label>
      <select id="pref-now-size">${PAGE_SIZES.map((n) => html`<option value="${n}" ${n === prefs.nowSize() ? raw("selected") : ""}>${n}</option>`)}</select>
      <span class="small muted">${t("На странице «Эфир»; чем меньше, тем быстрее она открывается.")}</span>
    </div>
    <div class="field">
      <span>${t("Поиск по умолчанию")}</span>
      <label class="row"><input type="radio" name="pref-strict" value="0" ${prefs.strict() ? "" : raw("checked")}>
        ${t("Обычный: находит формы слова («робот» найдёт «роботы») и части слов")}</label>
      <label class="row"><input type="radio" name="pref-strict" value="1" ${prefs.strict() ? raw("checked") : ""}>
        ${t("Строгий: только слово целиком, как написано")}</label>
      <span class="small muted">${t("На странице поиска режим можно переключить для одного запроса.")}</span>
    </div>
    <div class="field">
      <label for="pref-relevance">${t("Точность поиска передач")}</label>
      <select id="pref-relevance">${RELEVANCE.map(([v, label]) => html`<option value="${v}" ${v === prefs.relevance() ? raw("selected") : ""}>${label}</option>`)}</select>
      <span class="small muted">${t("«Только лучшие» убирает передачи, где слово нашлось лишь в описании, если есть совпадения в названии.")}</span>
    </div>
    <label class="row field"><input type="checkbox" id="pref-playable" ${prefs.playableOnly() ? raw("checked") : ""}>
      ${t("Скрывать каналы и передачи, у которых нет рабочих потоков")}</label>
    <label class="row field"><input type="checkbox" id="logos" ${prefs.logos() ? raw("checked") : ""}>
      ${t("Показывать логотипы каналов (картинки грузятся с сайтов из плейлиста)")}</label>
    <p class="small muted">${t("Эти настройки хранятся в этом браузере и действуют со следующего поиска или открытия страницы.")}</p>
    <h2>${t("История поиска")}</h2>
    <label class="row field"><input type="checkbox" id="pref-history" ${historyOn() ? raw("checked") : ""}>
      ${t("Запоминать мои поиски и показывать их списком под строкой поиска")}</label>
    <div class="row field"><span class="small muted" id="history-count">${t("Сохранено запросов: {0}", searchHistory().length)}</span>
      <button class="btn" type="button" id="history-clear" ${searchHistory().length ? "" : raw("disabled")}>${t("Очистить историю поиска")}</button></div>
    <div class="row field"><span class="small muted" id="channel-history-count">${t("Сохранено фильтров каналов: {0}", channelHistory().length)}</span>
      <button class="btn" type="button" id="channel-history-clear" ${channelHistory().length ? "" : raw("disabled")}>${t("Очистить историю каналов")}</button></div>
    <h2>${t("Подсказки")}</h2>
    <div class="row field"><button class="btn" type="button" id="tips-reset">${t("Показать все подсказки снова")}</button></div>
    <h2>${t("Сервер")}</h2>
    <div class="kv small">
      <span class="muted">${t("Версия")}</span><span>${health?.version || "—"}</span>
      <span class="muted">${t("Часовой пояс браузера")}</span><span>${TZ}</span>
      ${metrics ? html`<span class="muted">${t("Каналов")}</span><span>${nf.format(metrics.channels)}</span>
        <span class="muted">${t("С программой")}</span><span>${nf.format(metrics.channels_with_epg)}${metrics.epg_coverage != null ? ` (${Math.round(metrics.epg_coverage * 100)}%)` : ""}</span>
        <span class="muted">${t("EPG ждёт решения")}</span><span>${metrics.epg_mappings_needs_review}</span>` : ""}
    </div>
    <h2>${t("Кнопка «На ТВ»")}</h2>
    ${adapters ? html`<div class="field">
      <label class="row"><input type="radio" name="adapter" value="agent" ${current === "agent" ? raw("checked") : ""}>
        ${t("Через агента: он берёт команду через MCP, включает ТВ и сообщает, если не заиграло")}</label>
      <label class="row"><input type="radio" name="adapter" value="ha" ${current === "ha" ? raw("checked") : ""}
        ${adapters.available.includes("ha") ? "" : raw("disabled")}>
        ${t("Напрямую через Home Assistant{0}", adapters.ha_script ? t(": скрипт {0}", adapters.ha_script) : t(" (не настроен на сервере: MIH_HA_URL, MIH_HA_TOKEN)"))}</label>
      ${adapters.available.includes("ha") ? html`<div class="row"><button class="btn" type="button" id="ha-check">${t("Проверить связь с HA")}</button></div>` : ""}
      <span class="small muted">${t("Выбор хранится в этом браузере. Home Assistant получает адреса потоков и название канала и запускает свой скрипт, агент не участвует.")}</span>
    </div>` : ""}
    <h2>${t("Качество при запуске")}</h2>
    <div class="field">
      ${[["720", t("До 720p: быстрее старт, меньше буферизации (по умолчанию)")],
         ["auto", t("Авто: плеер выбирает сам")],
         ["best", t("Лучшее: самое высокое качество, дольше старт")]].map(([v, label]) =>
        html`<label class="row"><input type="radio" name="quality" value="${v}" ${(store.get("mih.quality") || "720") === v ? raw("checked") : ""}> ${label}</label>`)}
      <span class="small muted">${t("Для потоков с несколькими качествами сервер отдаёт телевизору подходящее. Выбор хранится в этом браузере.")}</span>
    </div>
    <p class="small"><a href="/api/docs">${t("Документация API")}</a></p>`;
}

// ---------- Status: what the service does, did and plans ----------

const JOB_NAMES = { sources: t("Обновление плейлистов"), epg: t("Обновление телепрограммы"),
  health: t("Проверка потоков"), alerts: t("Оповещения по темам"), discover: t("Поисковик плейлистов"),
  pack: t("Упаковщик плейлистов"), drive: t("Выгрузка на Google Диск"), digest: t("Сводка за сутки") };

function discoveryBlock(d) {
  if (!d || !d.found) return "";
  const st = d.by_status || {};
  return html`<h2>${t("Грабер плейлистов")}</h2>
    <div class="kv small">
      <span class="muted">${t("Найдено плейлистов")}</span><span>${t("{0} (репозиториев GitHub {1}, ждут разбора {2})", d.found, d.repos, d.repos_waiting)}</span>
      <span class="muted">${t("Ждут проверки")}</span><span>${st.new || 0}</span>
      <span class="muted">${t("Живые, ждут упаковщика")}</span><span>${st.live || 0}</span>
      <span class="muted">${t("Упакованы")}</span><span>${st.packed || 0}</span>
      <span class="muted">${t("Мёртвые / без новых каналов / копии")}</span><span>${st.dead || 0} / ${st.known || 0} / ${st.duplicate || 0}</span>
      <span class="muted">${t("Отклонены (логины, DRM, не плейлист)")}</span><span>${st.rejected || 0}</span>
      <span class="muted">${t("Каналов из найденного")}</span><span>${t("{0}, рабочих потоков {1} из {2}", d.auto_channels, d.auto_working, d.auto_streams)}</span>
    </div>`;
}
const LEVEL_ICON = { routine: "·", info: "●", warning: "▲", error: "✖" };
const JOB_STATUS = { ok: t("успешно"), warning: t("с предупреждениями"), error: t("ошибка") };

function whenShort(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const day = dayLabel(d);
  return `${day === t("Сегодня") ? "" : day + ", "}${time(iso)}`;
}

function everyText(min) {
  if (min % 1440 === 0) return min === 1440 ? t("сутки") : t("{0} дн.", min / 1440);
  if (min % 60 === 0) return t("{0} ч", min / 60);
  return t("{0} мин", min);
}

function eventItem(e) {
  return html`<li class="event ${e.level}">
    <span class="lvl" aria-hidden="true">${LEVEL_ICON[e.level] || "·"}</span>
    <div><div><time class="muted">${whenShort(e.at)}</time> ${e.title}</div>
    ${e.body ? html`<div class="small muted description">${e.body}</div>` : ""}</div></li>`;
}

async function viewStatus() {
  const s = await api(`/status?lang=${LANG}`);
  const running = s.running.length
    ? html`<ul class="plain">${s.running.map((r) => html`<li><span class="live">●</span> ${t("{0}, с {1}", JOB_NAMES[r.job] || r.job, time(r.since))}</li>`)}</ul>`
    : html`<p class="muted">${t("Сейчас ничего не выполняется.")}</p>`;
  const n = s.notifications;
  return html`${heading(t("Статус"))}
    <div class="kv small">
      <span class="muted">${t("Версия")}</span><span>${s.version}</span>
      <span class="muted">${t("Запущен")}</span><span>${whenShort(s.started_at)}</span>
      <span class="muted">${t("Планировщик")}</span><span>${s.scheduler ? t("включён") : t("выключен (MIH_SCHEDULER=0)")}</span>
      <span class="muted">${t("Пуши")}</span><span>${n.channel ? t("ntfy, от уровня «{0}»", n.level) : t("не настроены (MIH_NTFY_URL)")}</span>
    </div>
    ${n.channel ? html`<p><button class="btn" type="button" id="push-test">${t("Отправить тестовый пуш")}</button></p>` : ""}
    <h2>${t("Сейчас")}</h2>${running}
    <h2>${t("Задачи")}</h2>
    <div class="grid">${s.jobs.map((j) => html`<article class="card">
      <div class="body"><div class="title">${JOB_NAMES[j.name] || j.name}</div>
        <div class="small">${j.last_summary || t("ещё не запускалась")}</div>
        <div class="small muted">${j.last_finished_at ? t("Последний запуск: {0}, {1}", whenShort(j.last_finished_at), JOB_STATUS[j.last_status] || j.last_status) : ""}</div>
        <div class="small muted">${t("Следующий: {0}", j.running_since ? t("выполняется сейчас") : (j.next_run_at ? (Date.parse(j.next_run_at) <= Date.now() ? t("в ближайшую минуту") : whenShort(j.next_run_at)) : t("только вручную")))}</div></div>
      <div class="actions"><button class="btn" type="button" data-run-job="${j.name}" ${j.running_since ? raw("disabled") : ""}>${t("Запустить")}</button></div>
    </article>`)}</div>
    ${discoveryBlock(s.discovery)}
    <h2>${t("План на сутки")}</h2>
    ${s.plan.length ? html`<ul class="schedule">${s.plan.map((p) => html`<li><time>${time(p.next_at)}</time><span>${JOB_NAMES[p.job] || p.job}${p.every_minutes && p.runs.length > 4
        ? t(", каждые {0}", everyText(p.every_minutes))
        : (p.runs.length > 1 ? t(", затем {0}", p.runs.slice(1).map(time).join(", ")) : (dayLabel(new Date(p.next_at)) === t("Сегодня") ? "" : `, ${dayLabel(new Date(p.next_at)).toLowerCase()}`))}</span></li>`)}</ul>`
      : html`<p class="muted">${s.scheduler ? t("На ближайшие сутки ничего не запланировано.") : t("Планировщик выключен: задачи запускаются только вручную.")}</p>`}
    <h2>${t("События")}</h2>
    ${s.events.length ? html`<ul class="events">${s.events.map(eventItem)}</ul>` : html`<div class="empty">${t("Событий пока нет.")}</div>`}`;
}

// ---------- Coverage ----------

const nf = new Intl.NumberFormat(LOCALE);
const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : "—");
let regionNames = null;
try { regionNames = new Intl.DisplayNames([LANG], { type: "region" }); } catch { /* old browser */ }
function countryName(cc) {
  if (cc === "?") return t("Страна не указана");
  try { return regionNames?.of(cc) || cc; } catch { return cc; }
}
const REASONS = {
  no_stream: t("Нет ссылки на поток (канал только из программы)"), gone: t("Поток удалён"), no_answer: t("Не отвечает"),
  bad_manifest: t("Битый плейлист потока"), geo_suspect: t("Похоже на гео-блок"), server_error: t("Ошибка сервера"),
  unchecked: t("Ещё не проверен"), drm: t("Защищён DRM"), bad_content: t("Не видео"), bad_url: t("Неверная ссылка"),
  player_error: t("Не играет на ТВ"), absent: t("Пропал из плейлиста"), blocked_by_geo: t("Гео-блок"),
};
const DISCOVERY_STEPS = [
  ["urls_checked", t("Ссылок проверено"), false], ["repos", t("Репозиториев найдено"), false],
  ["found", t("Плейлистов найдено"), false], ["new", t("Ждут первой пробы"), false],
  ["live", t("Живые, ждут упаковщика"), true], ["packed", t("Упакованы"), true],
  ["dead", t("Мёртвые"), false], ["known", t("Без новых каналов"), false], ["duplicate", t("Копии"), false],
  ["rejected", t("Отклонены (логины, DRM)"), false],
  ["auto_streams", t("Потоков упаковано"), true], ["auto_channels", t("Каналов из упакованного"), true],
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
  if (!history || history.length < 2) return html`<p class="small muted">${t("График роста появится, когда наберётся хотя бы два дня наблюдений.")}</p>`;
  // Drawn at the panel's own width, so the labels keep their size on a phone and a desktop.
  const W = Math.max(280, Math.round(view.clientWidth - 58)), H = 200, L = 52, R = 8, T = 10, B = 24;
  const max = Math.max(...history.map((p) => p.all)) || 1;
  const x = (i) => L + ((W - L - R) * i) / (history.length - 1);
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const line = (k) => history.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[k]).toFixed(1)}`).join("");
  const ticks = [0, 0.5, 1].map((f) => Math.round(max * f));
  const series = [["all", "s-all", t("все")], ["working", "s-work", t("живые")], ["guide", "s-guide", t("живые с программой")]];
  const fmtD = (d) => new Date(d + "T00:00:00Z").toLocaleDateString(LOCALE, { day: "numeric", month: "short", timeZone: "UTC" });
  return html`<svg class="growth" viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="${t("Рост каталога по дням")}">
    ${ticks.map((v) => html`<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"></line><text x="${L - 6}" y="${y(v)}" dy="0.35em" text-anchor="end">${nf.format(v)}</text>`)}
    ${series.map(([k, cls]) => html`<path class="${cls}" d="${line(k)}"></path>`)}
    <text x="${L}" y="${H - 6}">${fmtD(history[0].day)}</text>
    <text x="${W - R}" y="${H - 6}" text-anchor="end">${fmtD(history[history.length - 1].day)}</text>
  </svg>${legend(series.map(([, cls, name]) => [cls, name]))}`;
}

async function viewCoverage(params) {
  const r = await api("/report");
  if (!r.funnel) return html`<h1>${t("Покрытие")}</h1><div class="empty">${t("Отчёт ещё не построен. Он появится после ближайшей проверки потоков.")}</div>`;
  const f = r.funnel, d = r.discovery || {}, st = d.by_status || {};
  const tile = (label, v, note) => html`<div class="tile"><span class="small muted">${label}</span><b>${nf.format(v)}</b><span class="small muted">${note}</span></div>`;

  const funnel = [["all", t("Все каналы в каталоге")], ["streams", t("Есть ссылка на поток")], ["working", t("Поток живой")], ["guide", t("Живой и с программой")]];
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
  const fmtDay = (s) => new Date(s + "T00:00:00Z").toLocaleDateString(LOCALE, { day: "numeric", month: "long", timeZone: "UTC" });
  const sources = r.sources || [];
  const smax = Math.max(...sources.map((x) => x.streams), 1);
  const g = r.guides || {}, ap = g.autopilot || {};

  return html`<h1>${t("Покрытие")}</h1>
    <p class="small muted">${t("Обновлено: {0}. Живой значит: поток отвечал при последней проверке.", whenShort(r.generated_at))}</p>
    <div class="tiles">
      ${tile(t("Все каналы"), f.all, t("в каталоге"))}
      ${tile(t("Со ссылкой на поток"), f.streams, pct(f.streams, f.all) + t(" от всех"))}
      ${tile(t("Живые"), f.working, pct(f.working, f.all) + t(" от всех"))}
      ${tile(t("Живые с программой"), f.guide, pct(f.guide, f.working) + t(" от живых"))}
      ${tile(t("Найдено грабером"), d.auto_channels || 0, t("каналов"))}
    </div>

    <h2>${t("Воронка каталога")}</h2>
    <div class="panel bars">${funnel.map(([k, label], i) => barRow(label, [[f[k], i === 3 ? "seg-guide" : "seg-step", t("каналов")]], f.all, `${nf.format(f[k])} · ${pct(f[k], f.all)}`))}</div>

    <h2>${t("Рост каталога")}</h2>
    <div class="panel">${growthChart(r.history)}</div>

    <h2>${t("По странам")}</h2>
    <div class="row">
      <a class="chip ${sort === "all" ? "on" : ""}" href="${link({ sort: "" })}">${t("Больше всего каналов")}</a>
      <a class="chip ${sort === "gap" ? "on" : ""}" href="${link({ sort: "gap" })}">${t("Больше всего без программы")}</a>
    </div>
    <div class="panel bars">
      ${legend([["seg-guide", t("живой, есть программа")], ["seg-work", t("живой, без программы")], ["seg-dead", t("ссылка есть, не отвечает")], ["seg-none", t("нет ссылки")]])}
      <p class="small muted">${t("Справа: доля живых каналов страны, у которых есть программа.")}</p>
      ${shown.map((c) => barRow(countryName(c.country), [[c.guide, SEG[0], t("с программой")], [c.working - c.guide, SEG[1], t("без программы")],
        [Math.max(0, c.streams - c.working), SEG[2], t("не отвечает")], [Math.max(0, c.all - c.streams), SEG[3], t("нет ссылки")]], cmax,
        c.working ? pct(c.guide, c.working) : "—"))}
      ${countries.length > 30 ? html`<a class="btn more" href="${link({ all: showAll ? "" : "1" })}">${showAll ? t("Только первые 30") : t("Все страны ({0})", countries.length)}</a>` : ""}
    </div>

    <h2>${t("Поисковик и упаковщик")}</h2>
    <div class="panel bars">${steps.map(([label, v, hot]) => barRow(label, [[v, hot ? "seg-guide" : "seg-step", label]], dmax, nf.format(v)))}</div>

    <h2>${t("Новые потоки по дням")}</h2>
    <div class="panel bars">
      ${legend([["seg-guide", t("живые сейчас")], ["seg-none", t("не отвечают")]])}
      ${days.length ? days.map((x) => barRow(fmtDay(x.day), [[x.working, "seg-guide", t("живые")], [x.streams - x.working, "seg-none", t("не отвечают")]], daymax, nf.format(x.streams)))
        : html`<p class="small muted">${t("За последние 30 дней новых потоков не было.")}</p>`}
    </div>

    <h2>${t("Потоки по источникам")}</h2>
    <div class="panel bars">
      ${legend([["seg-guide", t("живые")], ["seg-none", t("не отвечают")]])}
      ${sources.map((x) => barRow(x.auto ? t("Найдено грабером ({0} плейл.)", nf.format(x.playlists)) : x.name,
        [[x.working, "seg-guide", t("живые")], [x.streams - x.working, "seg-none", t("не отвечают")]], smax, nf.format(x.streams)))}
    </div>

    <h2>${t("Почему канал не попал в живые")}</h2>
    <div class="panel bars">${reasons.length ? reasons.map(([k, n]) => barRow(REASONS[k] || k, [[n, k === "no_stream" ? "seg-none" : "seg-dead", t("каналов")]], rmax, nf.format(n)))
      : html`<p class="small muted">${t("Все каналы живые.")}</p>`}</div>

    <h2>${t("Программа передач")}</h2>
    <div class="kv small">
      <span class="muted">${t("Источников программы")}</span><span>${nf.format(g.sources || 0)}</span>
      <span class="muted">${t("Автопилот: оставлены / на пробе")}</span><span>${ap.kept || 0} / ${ap.trial || 0}</span>
      <span class="muted">${t("Автопилот: бесполезные / сломанные")}</span><span>${ap.useless || 0} / ${ap.broken || 0}</span>
    </div>`;
}

// ---------- Past searches and channel filters: a list under the field, kept in this browser ----------

const HISTORY_MAX = 30;
const historyOn = () => store.get("mih.historyOff") !== "1";

// Each field with a history: where it is kept, its list and the form a pick submits.
const HISTORIES = {
  "search-input": { key: "mih.history", box: "search-history", label: "Прошлые поиски", form: "search-form" },
  "channel-q": { key: "mih.channelHistory", box: "channel-history", label: "Прошлые фильтры каналов", form: "channel-filter" },
};

function pastEntries(key) {
  try {
    const v = JSON.parse(store.get(key) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string" && x) : [];
  } catch { return []; }
}

const sameQuery = (a, b) => a.toLocaleLowerCase("ru") === b.toLocaleLowerCase("ru");

function rememberEntry(key, q) {
  if (!q || !historyOn()) return;
  const rest = pastEntries(key).filter((x) => !sameQuery(x, q));
  store.set(key, JSON.stringify([q, ...rest].slice(0, HISTORY_MAX)));
}

function forgetEntry(key, q) {
  const rest = pastEntries(key).filter((x) => !sameQuery(x, q));
  store.set(key, rest.length ? JSON.stringify(rest) : null);
}

const searchHistory = () => pastEntries("mih.history");
const channelHistory = () => pastEntries("mih.channelHistory");
const searchInput = document.getElementById("search-input");
let historyPick = -1;
let historyField = null; // the field whose list is open

// The list shows what matches the typed text; it only fills the field and runs the form on
// a click or Enter, never by itself.
function showHistory(input) {
  const h = HISTORIES[input.id];
  const box = document.getElementById(h.box);
  if (!box) return;
  if (historyField && historyField !== input) hideHistory();
  const typed = input.value.trim().toLocaleLowerCase("ru");
  const items = pastEntries(h.key).filter((q) => !typed || q.toLocaleLowerCase("ru").includes(typed)).slice(0, 10);
  if (!items.length || (items.length === 1 && sameQuery(items[0], input.value.trim()))) { hideHistory(); return; }
  historyPick = Math.min(historyPick, items.length - 1);
  box.innerHTML = String(html`${items.map((q, i) => html`<div class="item ${i === historyPick ? "on" : ""}" role="option"
      aria-selected="${i === historyPick}"><button type="button" class="q" data-history="${q}">${q}</button>
      <button type="button" class="del" data-history-del="${q}" title="${t("Убрать из истории")}" aria-label="${t("Убрать «{0}» из истории", q)}">×</button></div>`)}
    <div class="foot small"><span class="muted">${t(h.label)}</span><button type="button" data-history-clear>${t("Очистить историю")}</button></div>`);
  box.hidden = false;
  input.setAttribute("aria-expanded", "true");
  historyField = input;
}

function hideHistory() {
  historyPick = -1;
  if (!historyField) return;
  const box = document.getElementById(HISTORIES[historyField.id].box);
  if (box) box.hidden = true;
  historyField.setAttribute("aria-expanded", "false");
  historyField = null;
}

function clearHistory(key) {
  store.set(key, null);
  hideHistory();
  toast(t("История очищена"));
}

document.addEventListener("focusin", (e) => { if (HISTORIES[e.target.id]) showHistory(e.target); });
document.addEventListener("input", (e) => { if (HISTORIES[e.target.id]) { historyPick = -1; showHistory(e.target); } });
document.addEventListener("focusout", (e) => { const f = e.target; if (f === historyField) setTimeout(() => { if (historyField === f) hideHistory(); }, 150); });
document.addEventListener("keydown", (e) => {
  const input = e.target;
  if (!HISTORIES[input.id]) return;
  if (historyField !== input) { if (e.key === "ArrowDown") showHistory(input); return; }
  const rows = document.getElementById(HISTORIES[input.id].box).querySelectorAll(".item");
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    // -1 is the typed text itself: the arrows cycle through it and the list.
    const n = rows.length;
    if (e.key === "ArrowDown") historyPick = historyPick + 1 >= n ? -1 : historyPick + 1;
    else historyPick = historyPick <= -1 ? n - 1 : historyPick - 1;
    rows.forEach((r, i) => { r.classList.toggle("on", i === historyPick); r.setAttribute("aria-selected", String(i === historyPick)); });
  } else if (e.key === "Enter" && historyPick >= 0) {
    input.value = rows[historyPick].querySelector(".q").dataset.history;
  } else if (e.key === "Escape") {
    hideHistory();
  }
});
// Clicks inside a list must not blur the field first.
document.addEventListener("mousedown", (e) => { if (e.target.closest(".history")) e.preventDefault(); });
document.addEventListener("click", (e) => {
  if (!historyField || !e.target.closest(".history")) return;
  const input = historyField, h = HISTORIES[input.id];
  const pick = e.target.closest("[data-history]");
  const del = e.target.closest("[data-history-del]");
  if (pick) {
    input.value = pick.dataset.history;
    hideHistory();
    document.getElementById(h.form).requestSubmit();
  } else if (del) {
    forgetEntry(h.key, del.dataset.historyDel);
    showHistory(input);
    input.focus();
  } else if (e.target.closest("[data-history-clear]")) {
    clearHistory(h.key);
  }
});

// ---------- Router ----------

let renderSeq = 0;
function render(content) { view.innerHTML = String(content); }

// First-visit tips: one card per page, gone after «Понятно» until reset in Settings.
const TIPS = {
  home: "Здесь то, что идёт прямо сейчас. «На ТВ» включает канал на телевизоре, ★ добавляет его в избранное. Список не обновляется сам: нажмите «↻ Обновить», когда нужно.",
  search: "Пишите обычными словами, можно со временем и языком: «футбол завтра», «новости вечером», «роботы на русском». Поиск запускается по Enter или кнопке «Найти». Прошлые поиски появятся списком под строкой поиска.",
  channels: "Все каналы каталога. Введите часть названия, выберите страну и нажмите «Показать». Откройте канал, чтобы увидеть его программу на неделю.",
  favorites: "Здесь каналы и передачи, отмеченные ★. Добавьте тему, например «футбол», и перед началом такой передачи придёт пуш.",
  coverage: "Сколько каналов в каталоге, сколько из них сейчас работает и у скольких есть программа передач. Обновляется после каждой проверки потоков.",
  status: "Что делает сервер: фоновые задачи, их расписание и последние события. Задачу можно запустить вручную кнопкой «Запустить».",
  settings: "Настройки хранятся в этом браузере. Здесь меняются язык, размер выдачи и режим поиска, и отсюда можно вернуть подсказки.",
};

function tipCard(page) {
  if (!TIPS[page] || store.get("mih.tips." + page)) return "";
  return html`<aside class="tip" role="note"><div><b>${t("Как пользоваться")}</b><p>${t(TIPS[page])}</p></div>
    <button class="btn" type="button" data-tip-ok="${page}">${t("Понятно")}</button></aside>`;
}

function resetTips() { Object.keys(TIPS).forEach((page) => store.set("mih.tips." + page, null)); }

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
  else fn = async () => html`<div class="empty">${t("Страница не найдена.")} <a href="#/">${t("На главную")}</a></div>`;
  setTab(tab);
  try {
    await checkVersion();
    const out = await fn();
    if (seq === renderSeq) {
      render(parts.length > 1 ? out : html`${tipCard(parts[0] || "home")}${out}`);
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
  rememberEntry("mih.history", q);
  hideHistory();
  const cur = new URLSearchParams(location.hash.split("?")[1] || "");
  const keep = location.hash.startsWith("#/search") ? Object.fromEntries(SEARCH_KEYS.map((k) => [k, cur.get(k)])) : {};
  go("/search", { q, ...keep, run: "1" });
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
    fresh = true;
    route().finally(() => { fresh = false; });
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
      .then(() => { toast(t("Тема «{0}» убрана", delTopic.dataset.topicDel)); route(); })
      .catch((err) => toast(errorText(err), 5000));
    return;
  }
  const runBtn = e.target.closest("[data-run-job]");
  if (runBtn) {
    runBtn.disabled = true;
    api(`/jobs/${enc(runBtn.dataset.runJob)}/run`, { method: "POST" })
      .then(() => { toast(t("Задача запущена. Результат появится в событиях и придёт пушем.")); setTimeout(route, 1500); })
      .catch((err) => { runBtn.disabled = false; toast(errorText(err), 5000); });
    return;
  }
  if (e.target.id === "push-test") {
    api("/notifications/test", { method: "POST" })
      .then((r) => toast(r.result === "sent" ? t("Тестовый пуш отправлен") : t("Пуш не отправился: проверьте MIH_NTFY_URL"), 5000))
      .catch((err) => toast(errorText(err), 5000));
    return;
  }
  if (e.target.id === "ha-check") {
    e.target.disabled = true;
    api("/playback/ha/check", { method: "POST" })
      .then((r) => toast(r.ok ? t("Home Assistant отвечает, скрипт {0} найден", r.script) : (PLAY_TEXT[r.error] || t("Ошибка: {0}", r.error)), 6000))
      .catch((err) => toast(errorText(err), 5000))
      .finally(() => { e.target.disabled = false; });
    return;
  }
  if (e.target.id === "tips-reset") { resetTips(); toast(t("Подсказки снова появятся на страницах")); return; }
  const tipOk = e.target.closest("[data-tip-ok]");
  if (tipOk) { store.set("mih.tips." + tipOk.dataset.tipOk, "1"); tipOk.closest(".tip").remove(); return; }
  if (e.target.id === "history-clear") {
    clearHistory("mih.history");
    e.target.disabled = true;
    document.getElementById("history-count").textContent = t("Сохранено запросов: {0}", 0);
    return;
  }
  if (e.target.id === "channel-history-clear") {
    clearHistory("mih.channelHistory");
    e.target.disabled = true;
    document.getElementById("channel-history-count").textContent = t("Сохранено фильтров каналов: {0}", 0);
    return;
  }
  if (e.target.id === "token-clear") {
    store.set("mih.token", null);
    state.channelsLoaded = false;
    toast(t("Токен удалён из браузера"));
    route();
  }
});

document.addEventListener("submit", (e) => {
  if (e.target.id === "token-form") {
    e.preventDefault();
    const v = e.target.token.value.trim();
    if (!v) { toast(t("Вставьте токен в поле")); return; }
    store.set("mih.token", v);
    state.channelsLoaded = false;
    state.channels.clear();
    // Check it at once: a wrong token should say so here, not on the next screen.
    api("/health/ready")
      .then(() => { toast(t("Токен сохранён")); location.hash = "#/"; })
      .catch((err) => {
        if (err instanceof ApiFailure && err.status === 401) {
          store.set("mih.token", null);
          toast(t("Токен не подошёл. Скопируйте MIH_API_TOKEN целиком, без пробелов"), 6000);
        } else toast(errorText(err), 5000);
        route();
      });
  } else if (e.target.id === "channel-filter") {
    e.preventDefault();
    const f = new FormData(e.target);
    const q = (f.get("q") || "").trim();
    rememberEntry("mih.channelHistory", q);
    hideHistory();
    go("/channels", { q, country: f.get("country"), epg: f.get("epg") });
  } else if (e.target.id === "topic-form") {
    e.preventDefault();
    const topic = e.target.topic.value.trim();
    if (topic) followTopic(topic).then(route).catch((err) => toast(errorText(err), 5000));
  }
});

// Settings save as they change; nothing on screen reloads by itself.
const PREF_KEYS = { "pref-size": "mih.pageSize", "pref-now-size": "mih.nowSize", "pref-relevance": "mih.relevance" };
document.addEventListener("change", (e) => {
  const el = e.target;
  if (el.id === "pref-lang") { store.set("mih.lang", el.value); location.reload(); return; }
  if (el.dataset.filterSelect) { // like a chip: only marks the choice
    const cur = new URLSearchParams(location.hash.split("?")[1] || "");
    if (el.value) cur.set(el.dataset.filterSelect, el.value); else cur.delete(el.dataset.filterSelect);
    history.replaceState(null, "", "#/search?" + cur);
    return;
  }
  if (PREF_KEYS[el.id]) { store.set(PREF_KEYS[el.id], el.value); toast(t("Сохранено")); }
  else if (el.name === "pref-strict") { store.set("mih.strict", el.value); toast(t("Сохранено")); }
  else if (el.id === "pref-playable") { store.set("mih.playable", el.checked ? "1" : "0"); toast(t("Сохранено")); }
  else if (el.id === "pref-history") {
    store.set("mih.historyOff", el.checked ? null : "1");
    toast(el.checked ? t("Поиски будут запоминаться") : t("Новые поиски больше не запоминаются"));
  }
});

document.addEventListener("input", (e) => {
  if (e.target.name === "adapter") {
    store.set("mih.adapter", e.target.value);
    toast(e.target.value === "ha" ? t("«На ТВ» теперь идёт напрямую в Home Assistant") : t("«На ТВ» теперь выполняет агент"));
  }
  if (e.target.name === "quality") {
    store.set("mih.quality", e.target.value);
    toast(t("Сохранено"));
  }
  if (e.target.id === "logos") {
    store.set("mih.logos", e.target.checked ? "1" : "0");
    toast(t("Сохранено"));
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
