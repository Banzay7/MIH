// Interface language. Russian is the source text: the keys are the Russian strings
// themselves, so a missing translation falls back to English, then to Russian, never to
// a blank. Each language's texts are in i18n/<code>.js; this file loads the one the
// visitor needs (plus English under the others) and then the app.
"use strict";

// The order of the language menu: the three main languages first, then the most spoken.
const LANGS_MAIN = ["ru", "en", "vi"];
const LANGS_UI = {
  ru: "Русский", en: "English", vi: "Tiếng Việt",
  zh: "中文（简体）", hi: "हिन्दी", es: "Español", fr: "Français", ar: "العربية",
  bn: "বাংলা", pt: "Português", ur: "اردو", id: "Bahasa Indonesia", de: "Deutsch",
  ja: "日本語", mr: "मराठी", te: "తెలుగు", tr: "Türkçe", ta: "தமிழ்", ko: "한국어",
  it: "Italiano",
};
const LOCALES = {
  ru: "ru-RU", en: "en-GB", vi: "vi-VN", zh: "zh-CN", hi: "hi-IN", es: "es-ES", fr: "fr-FR",
  ar: "ar", bn: "bn-BD", pt: "pt-BR", ur: "ur-PK", id: "id-ID", de: "de-DE", ja: "ja-JP",
  mr: "mr-IN", te: "te-IN", tr: "tr-TR", ta: "ta-IN", ko: "ko-KR", it: "it-IT",
};
const RTL = ["ar", "ur"];

const LANG = (() => {
  let saved = null;
  try { saved = localStorage.getItem("mih.lang"); } catch { /* private mode */ }
  if (saved && LANGS_UI[saved]) return saved;
  const nav = (navigator.languages || [navigator.language || ""]).map((l) => String(l).slice(0, 2).toLowerCase());
  return nav.find((l) => LANGS_UI[l]) || "ru";
})();
const LOCALE = LOCALES[LANG];
// The server words its events and job summaries in Russian, English and Vietnamese; the
// other interface languages read them in English.
const SERVER_LANG = LANGS_MAIN.includes(LANG) ? LANG : "en";

const DICTS = (self.MIH_DICTS ||= {});

// t("Найдено: {0}", n): look the Russian text up and put the arguments in.
function t(key, ...args) {
  const s = DICTS[LANG]?.[key] ?? DICTS.en?.[key] ?? key;
  return args.length ? s.replace(/\{(\d+)\}/g, (m, i) => (args[i] ?? "")) : s;
}

// The page shell: elements marked data-i18n get their text, data-i18n-* their attributes.
// app.js calls it first, once the texts are loaded.
function translateShell() {
  document.documentElement.lang = LANG;
  document.documentElement.dir = RTL.includes(LANG) ? "rtl" : "ltr";
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  for (const attr of ["placeholder", "aria-label", "title"]) {
    document.querySelectorAll(`[data-i18n-${attr}]`).forEach((el) => el.setAttribute(attr, t(el.getAttribute(`data-i18n-${attr}`))));
  }
}

// The texts, then the app, in this order (async=false keeps it).
for (const src of [...(LANG === "ru" ? [] : LANG === "en" || LANG === "vi" ? [LANG] : ["en", LANG])
  .map((code) => `i18n/${code}.js`), "app.js"]) {
  const el = document.createElement("script");
  el.src = src;
  el.async = false;
  document.head.appendChild(el);
}
