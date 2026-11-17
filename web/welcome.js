/* Welcome screen. Runs both inside the Samsung TV app and in a normal browser.
 * Written in ES5 so it works on older Tizen TVs (2017+). */
(function () {
  "use strict";

  var SERVER = (window.WELCOME_SERVER || "").replace(/\/$/, "");
  var REFRESH_MS = 60 * 1000;
  var KEY = { LEFT: 37, UP: 38, RIGHT: 39, DOWN: 40, ENTER: 13, BACK: 10009, RED: 403 };
  var isTizen = typeof window.tizen !== "undefined";
  var data = null;
  var setupIndex = 0;

  function $(id) { return document.getElementById(id); }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }

  /* ---- scale the 1920x1080 stage to whatever the screen is ---- */
  function fit() {
    var s = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    var st = $("stage");
    st.style.transform = "scale(" + s + ")";
    st.style.left = (window.innerWidth - 1920 * s) / 2 + "px";
    st.style.top = (window.innerHeight - 1080 * s) / 2 + "px";
  }
  window.addEventListener("resize", fit);

  /* ---- which TV am I? ?tv=... in the URL, else saved choice, else ask ---- */
  function tvId() {
    var m = /[?&]tv=([^&]+)/.exec(location.search);
    if (m) { store("tvId", decodeURIComponent(m[1])); return decodeURIComponent(m[1]); }
    return store("tvId");
  }

  /* ---- network ---- */
  function getJSON(url, ok, fail) {
    var x = new XMLHttpRequest();
    x.open("GET", url, true);
    x.timeout = 10000;
    x.onload = function () {
      if (x.status === 200) { try { ok(JSON.parse(x.responseText)); } catch (e) { fail(e); } }
      else fail(new Error("HTTP " + x.status));
    };
    x.onerror = x.ontimeout = function () { fail(new Error("network")); };
    x.send();
  }

  function refresh() {
    var id = tvId() || "";
    getJSON(SERVER + "/api/welcome?tv=" + encodeURIComponent(id), function (d) {
      data = d;
      store("lastPayload", JSON.stringify(d));
      $("offline").hidden = true;
      if (!id) showSetup(); else render();
    }, function () {
      var cached = store("lastPayload");
      if (cached) { data = JSON.parse(cached); render(); $("offline").hidden = false; }
      else showSetupError();
    });
  }

  /* ---- rendering ---- */
  var WEATHER = { 0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow", 80: "Showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Snow showers", 95: "Thunderstorms", 96: "Thunderstorms", 99: "Thunderstorms" };
  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

  function text(id, v) { $(id).textContent = v == null ? "" : v; }
  function show(id, on) { $(id).hidden = !on; }

  function fmtTime(hhmm) {
    var p = (hhmm || "").split(":"), h = +p[0], m = p[1] || "00";
    if (isNaN(h)) return hhmm;
    return (h % 12 || 12) + (m === "00" ? "" : ":" + m) + (h < 12 ? " am" : " pm");
  }
  function fmtDate(iso) {
    var p = iso.split("-"), d = new Date(+p[0], +p[1] - 1, +p[2]);
    return DAYS[d.getDay()] + ", " + MONTHS[d.getMonth()].slice(0, 3) + " " + d.getDate();
  }

  function render() {
    if (!data) return;
    var p = data.property, g = data.guest, tv = data.tv, w = data.weather;
    show("setup", false);
    show("welcome", true);

    var bg = tv.background || p.background;
    if (bg) $("bg").style.backgroundImage = "url('" + bg.replace(/'/g, "%27") + "')";
    if (tv.accent) document.documentElement.style.setProperty("--accent", tv.accent);

    text("propertyName", p.name);
    if (g && g.first_name) {
      text("eyebrow", "Welcome to " + p.name);
      text("greeting", "Hi, " + g.first_name + ".");
    } else {
      text("eyebrow", "Welcome");
      text("greeting", p.name || "Welcome");
    }
    $("greeting").className = $("greeting").textContent.length > 14 ? "long" : "";
    text("message", tv.message);

    var notes = $("notes");
    notes.innerHTML = "";
    for (var i = 0; i < p.notes.length; i++) {
      var li = document.createElement("li");
      li.textContent = p.notes[i];
      notes.appendChild(li);
    }

    show("wifiCard", !!p.wifi_name);
    text("wifiName", p.wifi_name);
    text("wifiPass", p.wifi_password);
    $("wifiName").className = (p.wifi_name || "").length > 16 ? "long" : "";
    $("wifiPass").className = (p.wifi_password || "").length > 16 ? "long" : "";
    drawWifiQr(p);

    show("weatherCard", !!w);
    if (w) {
      text("temp", w.temp + w.unit);
      $("weatherDesc").innerHTML = "";
      $("weatherDesc").appendChild(document.createTextNode(WEATHER[w.code] || ""));
      $("weatherDesc").appendChild(document.createElement("br"));
      $("weatherDesc").appendChild(document.createTextNode("High " + w.high + "° · Low " + w.low + "°"));
    }

    text("checkoutTime", fmtTime(p.checkout_time));
    text("checkoutDate", g ? fmtDate(g.checkout) : "");

    show("hostCard", !!(p.host_name || p.host_phone));
    text("hostName", p.host_name ? "Text " + p.host_name : "");
    text("hostPhone", p.host_phone);
  }

  function wifiEscape(s) { return String(s).replace(/([\\;,:"])/g, "\\$1"); }
  var lastQr = "";
  function drawWifiQr(p) {
    if (!p.wifi_name || typeof qrcode === "undefined") { $("wifiQr").hidden = true; return; }
    var payload = "WIFI:T:" + (p.wifi_security || "WPA") + ";S:" + wifiEscape(p.wifi_name) +
      ";P:" + wifiEscape(p.wifi_password || "") + ";;";
    if (payload === lastQr) return;
    lastQr = payload;
    var qr = qrcode(0, "M");
    qr.addData(payload);
    qr.make();
    $("wifiQr").innerHTML = qr.createSvgTag({ cellSize: 6, margin: 0, scalable: true });
    $("wifiQr").hidden = false;
  }

  function tick() {
    var d = new Date(), h = d.getHours(), m = d.getMinutes();
    text("time", (h % 12 || 12) + ":" + (m < 10 ? "0" : "") + m + (h < 12 ? " am" : " pm"));
    text("date", DAYS[d.getDay()] + ", " + MONTHS[d.getMonth()] + " " + d.getDate());
  }

  /* ---- setup: pick which TV this is ---- */
  function showSetup() {
    show("welcome", false);
    show("setup", true);
    var list = $("tvList"), tvs = (data && data.tvs) || [];
    list.innerHTML = "";
    var current = tvId();
    for (var i = 0; i < tvs.length; i++) {
      var li = document.createElement("li");
      li.textContent = tvs[i].label;
      li.setAttribute("data-id", tvs[i].id);
      if (tvs[i].id === current) setupIndex = i;
      list.appendChild(li);
    }
    text("setupStatus", tvs.length ? "" : "No TVs are listed in the server's config.json.");
    focusSetup();
  }
  function showSetupError() {
    show("welcome", false);
    show("setup", true);
    $("tvList").innerHTML = "";
    text("setupStatus", "Can't reach the welcome server at " + (SERVER || location.host) + ". Retrying…");
  }
  function focusSetup() {
    var items = $("tvList").children;
    for (var i = 0; i < items.length; i++) items[i].className = i === setupIndex ? "focused" : "";
  }
  function inSetup() { return !$("setup").hidden && $("tvList").children.length > 0; }

  /* ---- remote control ---- */
  if (isTizen) {
    try { tizen.tvinputdevice.registerKey("ColorF0Red"); } catch (e) { /* older firmware */ }
  }
  document.addEventListener("keydown", function (e) {
    var items = $("tvList").children;
    if (inSetup()) {
      if (e.keyCode === KEY.UP || e.keyCode === KEY.LEFT) { setupIndex = (setupIndex + items.length - 1) % items.length; focusSetup(); }
      else if (e.keyCode === KEY.DOWN || e.keyCode === KEY.RIGHT) { setupIndex = (setupIndex + 1) % items.length; focusSetup(); }
      else if (e.keyCode === KEY.ENTER) { store("tvId", items[setupIndex].getAttribute("data-id")); refresh(); }
      else if (e.keyCode === KEY.BACK && tvId()) { render(); }
      e.preventDefault();
      return;
    }
    if (e.keyCode === KEY.RED) { showSetup(); e.preventDefault(); }
    else if (e.keyCode === KEY.BACK && isTizen) {
      try { tizen.application.getCurrentApplication().exit(); } catch (err) { /* ignore */ }
    }
  });

  fit();
  tick();
  setInterval(tick, 10 * 1000);
  refresh();
  setInterval(function () { if (!inSetup()) refresh(); }, REFRESH_MS);
  // When the app comes back to the foreground, refresh right away
  document.addEventListener("visibilitychange", function () { if (!document.hidden && !inSetup()) refresh(); });
})();
