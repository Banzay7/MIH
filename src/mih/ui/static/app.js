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
  for (let offset = 0; offset < 5000; offset += 500) {
    const page = await api(`/channels?limit=500&offset=${offset}`);
    page.items.forEach((c) => state.channels.set(c.id, c));
    if (page.items.length < 500) break;
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
  accepted: "Команда отправлена на ТВ…",
  started: "Запущено на ТВ",
  no_adapter: "Поток выбран, но запуск на ТВ ещё не подключён: он появится с интеграцией Home Assistant.",
  no_stream: "У этого канала сейчас нет рабочего потока.",
};

async function playOnTv(channelId, feedId, button) {
  if (button) button.disabled = true;
  try {
    const key = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    const target = { kind: "channel", channel_id: channelId };
    if (feedId) target.feed_id = feedId;
    let cmd = await api("/playback/commands", { method: "POST", body: { target, idempotency_key: key } });
    for (let i = 0; cmd.state === "accepted" && i < 10; i++) {
      toast(PLAY_TEXT.accepted);
      await new Promise((r) => setTimeout(r, 1000));
      cmd = await api(`/playback/commands/${enc(cmd.id)}`);
    }
    if (cmd.state === "started") toast(PLAY_TEXT.started);
    else if (cmd.state === "failed") toast(PLAY_TEXT[cmd.error] || `Не удалось запустить: ${cmd.error}`, 6000);
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

function logo(channelId, name, big = false) {
  const ch = state.channels.get(channelId);
  const url = ch?.logo_url;
  const cls = big ? "logo big" : "logo";
  if (url && /^https?:\/\//i.test(url) && store.get("mih.logos", "1") === "1") {
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

// ---------- Cards ----------

function nowCard(item) {
  const name = item.channel_name || state.channels.get(item.channel_id)?.canonical_name || "Канал";
  const cur = item.now, nxt = item.next;
  return html`<article class="card">
    ${logo(item.channel_id, name)}
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
    ${logo(a.channel_id, name)}
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

async function viewHome() {
  render(html`<h1>Сейчас в эфире</h1>${skeleton()}`);
  await Promise.all([loadChannels(), loadFavorites()]);
  const data = await api("/now");
  if (!state.channels.size) {
    return html`<h1>Сейчас в эфире</h1>
      <div class="empty"><p>Каталог пока пуст.</p>
      <p class="small">Добавьте свой плейлист и программу передач (EPG): командой
      <code>mih import-file</code> или через API, см. <a href="/api/docs">документацию API</a>.</p></div>`;
  }
  const favIds = new Set([...state.favorites].filter((k) => k.startsWith("channel:")).map((k) => k.slice(8)));
  const onAir = data.items.filter((i) => i.now);
  const favNow = onAir.filter((i) => favIds.has(i.channel_id));
  const soonLimit = Date.now() + 45 * 60000;
  const soon = data.items.filter((i) => i.next && Date.parse(i.next.start) <= soonLimit)
    .sort((a, b) => Date.parse(a.next.start) - Date.parse(b.next.start)).slice(0, 12);
  const noGuide = state.channels.size - new Set(data.items.map((i) => i.channel_id)).size;
  return html`
    <h1>Сейчас в эфире</h1>
    <p class="muted small">${onAir.length} в эфире · ${state.channels.size} каналов в каталоге${noGuide > 0 ? ` · без программы: ${noGuide}` : ""}</p>
    ${favNow.length ? html`<h2>Избранное</h2><div class="grid">${favNow.map(nowCard)}</div>` : ""}
    ${soon.length ? html`<h2>Скоро начнётся</h2><div class="grid">${soon.map((i) => airingCard({
      ...i.next, channel_name: i.channel_name }))}</div>` : ""}
    <h2>Все каналы в эфире</h2>
    ${onAir.length ? html`<div class="grid">${onAir.map(nowCard)}</div>`
      : html`<div class="empty">Нет данных о текущем эфире. Загрузите программу передач (EPG).</div>`}`;
}

const SEARCH_TIMES = [["", "Любое время"], ["now", "Сейчас"], ["today", "Сегодня"], ["tonight", "Вечером"], ["tomorrow", "Завтра"]];
const SEARCH_LANGS = [["", "Любой язык"], ["ru", "Русский"], ["en", "Английский"], ["vi", "Вьетнамский"]];
const SEARCH_TYPES = [["", "Всё"], ["airing", "Передачи"], ["channel", "Каналы"]];

function chipRow(name, options, current) {
  return html`<div class="row" role="group">${options.map(([v, label]) =>
    html`<button type="button" class="chip ${v === current ? "on" : ""}" data-filter="${name}" data-value="${v}"
      aria-pressed="${v === current}">${label}</button>`)}</div>`;
}

async function viewSearch(params) {
  const q = params.get("q") || "";
  const timeF = params.get("time") || "";
  const lang = params.get("lang") || "";
  const type = params.get("type") || "";
  document.getElementById("search-input").value = q;
  const filters = html`<div class="filters">
    ${chipRow("time", SEARCH_TIMES, timeF)}${chipRow("lang", SEARCH_LANGS, lang)}${chipRow("type", SEARCH_TYPES, type)}
  </div>`;
  if (!q && !timeF) {
    return html`<h1>Поиск</h1>${filters}
      <div class="empty"><p>Ищите передачи и каналы обычными словами.</p>
      <p class="small">Например: «новости вечером», «футбол завтра», «роботы на русском», «Первый канал».</p></div>`;
  }
  render(html`<h1>Поиск</h1>${filters}${skeleton(3)}`);
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const body = { query: q, tz: TZ, limit: 50, filters: {} };
  if (timeF) body.filters.time = timeF;
  if (lang) body.filters.languages = [lang];
  if (type) body.target_types = [type];
  const res = await api("/search", { method: "POST", body });
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
    ${channels.length ? html`<h2>Каналы</h2><div class="grid">${channels.map(channelCard)}</div>` : ""}
    ${airings.length ? html`<h2>Передачи</h2><div class="grid">${airings.map(airingCard)}</div>` : ""}
    ${!res.items.length ? html`<div class="empty">Ничего не нашлось. Попробуйте другие слова или уберите фильтры.</div>` : ""}`;
}

async function viewChannels(params) {
  const q = params.get("q") || "";
  const epg = params.get("epg") === "1";
  const country = params.get("country") || "";
  const shown = Number(params.get("n") || 60);
  render(html`<h1>Каналы</h1>${skeleton(6)}`);
  await Promise.all([loadChannels(), state.favoritesLoaded ? null : loadFavorites()]);
  const all = [...state.channels.values()];
  const countries = [...new Set(all.map((c) => c.country).filter(Boolean))].sort();
  const needle = q.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
  const list = all.filter((c) => (!epg || c.has_epg) && (!country || c.country === country) &&
    (!needle || c.canonical_name.toLocaleLowerCase("ru").replaceAll("ё", "е").includes(needle)));
  return html`<h1>Каналы</h1>
    <form class="filters row" id="channel-filter">
      <input name="q" type="search" value="${q}" placeholder="Название канала" aria-label="Фильтр по названию">
      ${countries.length ? html`<select name="country" aria-label="Страна">
        <option value="">Все страны</option>
        ${countries.map((c) => html`<option value="${c}" ${c === country ? raw("selected") : ""}>${c}</option>`)}
      </select>` : ""}
      <label class="row small"><input type="checkbox" name="epg" value="1" ${epg ? raw("checked") : ""}> только с программой</label>
    </form>
    <p class="small muted">Найдено: ${list.length}</p>
    ${list.length ? html`<div class="grid">${list.slice(0, shown).map(channelCard)}</div>`
      : html`<div class="empty">Нет каналов под эти условия.</div>`}
    ${list.length > shown ? html`<a class="btn more" href="${"#/channels?" + new URLSearchParams({ q, country, epg: epg ? "1" : "", n: String(shown + 60) })}">Показать ещё</a>` : ""}`;
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
      ${p.icon_url && /^https?:\/\//i.test(p.icon_url) && store.get("mih.logos", "1") === "1"
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

function topicsBlock(topics, upcoming) {
  return html`<h2>Темы</h2>
    <p class="small muted">Перед началом передачи по теме придёт пуш со ссылкой на запуск. Тема ищется в названии, подзаголовке и жанре передачи, с начала слова.</p>
    ${topics.length ? html`<div class="chips">${topics.map((t) => html`<button type="button" class="chip on" data-topic-del="${t.topic}"
      title="Убрать тему" aria-label="Убрать тему ${t.topic}">${t.topic} ×</button>`)}</div>` : ""}
    <form id="topic-form" class="row field">
      <input name="topic" minlength="2" maxlength="64" required autocomplete="off" aria-label="Новая тема"
        placeholder="Например: футбол, формула 1, роботы">
      <button class="btn tv" type="submit">Следить</button>
    </form>
    ${upcoming.length ? html`<h2>Скоро по вашим темам</h2><div class="grid">${upcoming.map(alertCard)}</div>`
      : topics.length ? html`<p class="muted small">В ближайшие сутки по вашим темам на рабочих каналах ничего нет.</p>` : ""}`;
}

async function viewFavorites() {
  render(html`<h1>Избранное</h1>${skeleton(3)}`);
  const [items, , topics, upcoming] = await Promise.all([loadFavorites(), loadChannels(),
    api(`/profiles/${PROFILE}/topics`), api(`/profiles/${PROFILE}/alerts/upcoming?hours=24`)]);
  const topicsHtml = topicsBlock(topics.items, upcoming.items);
  const channels = items.filter((f) => f.target_kind === "channel");
  const programmes = items.filter((f) => f.target_kind === "programme");
  let nowBy = new Map();
  const present = channels.filter((f) => f.name);
  if (present.length) {
    const qs = present.map((f) => `channel_id=${enc(f.target_id)}`).join("&");
    const data = await api(`/now?${qs}`);
    data.items.forEach((i) => { if (!nowBy.has(i.channel_id)) nowBy.set(i.channel_id, i); });
  }
  if (!items.length) {
    return html`<h1>Избранное</h1><div class="empty"><p>Здесь пока пусто.</p>
      <p class="small">Нажмите ★ у канала или передачи, чтобы добавить.</p></div>${topicsHtml}`;
  }
  return html`<h1>Избранное</h1>
    ${channels.length ? html`<h2>Каналы</h2><div class="grid">${channels.map((f) => {
      if (!f.name) {
        return html`<article class="card"><span class="logo" aria-hidden="true">?</span>
          <div class="body"><div class="title">Канал пропал из плейлистов</div>
          <div class="small muted">Избранное сохранено: канал вернётся, когда появится в источнике.</div></div>
          <div class="actions">${star("channel", f.target_id)}</div></article>`;
      }
      return nowCard(nowBy.get(f.target_id) || { channel_id: f.target_id, channel_name: f.name });
    })}</div>` : ""}
    ${programmes.length ? html`<h2>Передачи</h2><div class="grid">${programmes.map((f) => html`<article class="card">
      <div class="body"><a class="stretch title" href="#/programme/${enc(f.target_id)}">${f.name || "Передача удалена из программы"}</a></div>
      <div class="actions">${star("programme", f.target_id)}</div></article>`)}</div>` : ""}
    ${topicsHtml}`;
}

async function viewSettings() {
  let health = null, metrics = null, err = null;
  try { health = await api("/health/live"); } catch (e) { err = e; }
  try { metrics = await api("/metrics/summary"); } catch (e) { err = err || e; }
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
    <label class="row field"><input type="checkbox" id="logos" ${store.get("mih.logos", "1") === "1" ? raw("checked") : ""}>
      Показывать логотипы каналов (картинки грузятся с сайтов из плейлиста)</label>
    <h2>Сервер</h2>
    <div class="kv small">
      <span class="muted">Версия</span><span>${health?.version || "—"}</span>
      <span class="muted">Часовой пояс браузера</span><span>${TZ}</span>
      ${metrics ? html`<span class="muted">Каналов</span><span>${metrics.channels}</span>
        <span class="muted">С программой</span><span>${metrics.channels_with_epg}${metrics.epg_coverage != null ? ` (${Math.round(metrics.epg_coverage * 100)}%)` : ""}</span>
        <span class="muted">EPG ждёт решения</span><span>${metrics.epg_mappings_needs_review}</span>` : ""}
    </div>
    <p class="small muted">Запуск на ТВ пока работает как заглушка: команда записывается и поток выбирается, но на устройство ничего не отправляется. Интеграция с Home Assistant будет следующим шагом.</p>
    <p class="small"><a href="/api/docs">Документация API</a></p>`;
}

// ---------- Status: what the service does, did and plans ----------

const JOB_NAMES = { sources: "Обновление плейлистов", epg: "Обновление телепрограммы",
  health: "Проверка потоков", alerts: "Оповещения по темам", digest: "Сводка за сутки" };
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
  return html`<h1>Статус</h1>
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
    <h2>План на сутки</h2>
    ${s.plan.length ? html`<ul class="schedule">${s.plan.map((p) => html`<li><time>${time(p.next_at)}</time><span>${JOB_NAMES[p.job] || p.job}${p.every_minutes && p.runs.length > 4
        ? `, каждые ${everyText(p.every_minutes)}`
        : (p.runs.length > 1 ? `, затем ${p.runs.slice(1).map(time).join(", ")}` : (dayLabel(new Date(p.next_at)) === "Сегодня" ? "" : `, ${dayLabel(new Date(p.next_at)).toLowerCase()}`))}</span></li>`)}</ul>`
      : html`<p class="muted">${s.scheduler ? "На ближайшие сутки ничего не запланировано." : "Планировщик выключен: задачи запускаются только вручную."}</p>`}
    <h2>События</h2>
    ${s.events.length ? html`<ul class="events">${s.events.map(eventItem)}</ul>` : html`<div class="empty">Событий пока нет.</div>`}`;
}

// ---------- Router ----------

let renderSeq = 0;
function render(content) { view.innerHTML = String(content); }

function setTab(name) {
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === name));
}

async function route() {
  const seq = ++renderSeq;
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
  const cur = new URLSearchParams(location.hash.split("?")[1] || "");
  const keep = location.hash.startsWith("#/search") ? { time: cur.get("time"), lang: cur.get("lang"), type: cur.get("type") } : {};
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
  const chip = e.target.closest("[data-filter]");
  if (chip) {
    const cur = new URLSearchParams(location.hash.split("?")[1] || "");
    cur.set(chip.dataset.filter, chip.dataset.value);
    go("/search", Object.fromEntries(cur));
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
    if (v) store.set("mih.token", v);
    state.channelsLoaded = false;
    state.channels.clear();
    toast("Токен сохранён");
    route();
  } else if (e.target.id === "channel-filter") {
    e.preventDefault();
  } else if (e.target.id === "topic-form") {
    e.preventDefault();
    const topic = e.target.topic.value.trim();
    if (topic) followTopic(topic).then(route).catch((err) => toast(errorText(err), 5000));
  }
});

let filterTimer;
document.addEventListener("input", (e) => {
  const form = e.target.closest("#channel-filter");
  if (form) {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      const f = new FormData(form);
      const next = "#/channels?" + new URLSearchParams({ q: f.get("q") || "", country: f.get("country") || "", epg: f.get("epg") || "" });
      history.replaceState(null, "", next);
      const pos = e.target.selectionStart;
      route().then(() => {
        const input = document.querySelector('#channel-filter [name="q"]');
        if (input && e.target.name === "q") { input.focus(); input.setSelectionRange(pos, pos); }
      });
    }, e.target.type === "search" ? 250 : 0);
  }
  if (e.target.id === "logos") {
    store.set("mih.logos", e.target.checked ? "1" : "0");
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

// Live screens go stale: refresh "now" once a minute while the tab is visible.
setInterval(() => {
  const h = location.hash;
  if (document.visibilityState === "visible" && (h === "" || h === "#/" || h.startsWith("#/favorites"))) route();
}, 60000);
// The status screen follows running jobs more closely.
setInterval(() => {
  if (document.visibilityState === "visible" && location.hash.startsWith("#/status")) route();
}, 15000);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => { /* http on a LAN address: no PWA, still works */ });
}

route();
