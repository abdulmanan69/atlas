/*
 * Website voice widget.
 *
 *   <script src="https://<your-api>/api/v1/public/embed/widget.js"
 *           data-agent-key="wk_..." async></script>
 *
 * Optional: data-label="Talk to us", data-position="right|left", data-color="#2563eb".
 *
 * Asks the page's server-side API for a session (the site must be on the widget's allowed
 * list), then connects the visitor's microphone to the agent over WebRTC. Nothing here
 * holds credentials: the session is one-time and scoped to this widget.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script || !script.src) return;
  var key = script.getAttribute("data-agent-key");
  if (!key || window["__voiceWidget_" + key]) return;
  window["__voiceWidget_" + key] = true;

  var src = new URL(script.src, window.location.href);
  var base = src.origin + src.pathname.replace(/\/api\/v1\/public\/embed\/widget\.js$/, "");
  var api = base + "/api/v1/public/embed/" + encodeURIComponent(key);
  var label = script.getAttribute("data-label") || "Talk to us";
  var side = script.getAttribute("data-position") === "left" ? "left" : "right";
  var color = script.getAttribute("data-color") || "#1f6feb";
  if (!/^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|rgba?\([0-9.,\s%]+\))$/.test(color)) color = "#1f6feb";

  var MIC =
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 17v4"/></svg>';
  var CLOSE =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';

  var host = document.createElement("div");
  host.setAttribute("data-voice-widget", "");
  var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
  root.innerHTML =
    "<style>" +
    ":host{all:initial}" +
    ".w{position:fixed;bottom:20px;" + side + ":20px;z-index:2147483000;font:14px/1.4 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:#111}" +
    ".b{display:inline-flex;align-items:center;gap:8px;height:44px;padding:0 18px;border:0;border-radius:22px;background:" + color + ";color:#fff;font-weight:600;font-size:14px;line-height:1;font-family:inherit;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.18)}" +
    ".b:focus-visible,.a:focus-visible,.x:focus-visible{outline:2px solid #111;outline-offset:2px}" +
    ".p{display:none;width:280px;margin-bottom:12px;padding:16px;border-radius:14px;background:#fff;box-shadow:0 12px 40px rgba(0,0,0,.2);border:1px solid rgba(0,0,0,.08)}" +
    ".open .p{display:block}" +
    ".h{display:flex;align-items:start;justify-content:space-between;gap:8px}" +
    ".t{font-weight:600;font-size:15px;margin:0}" +
    ".s{margin:6px 0 14px;color:#555;font-size:13px;min-height:18px}" +
    ".x{border:0;background:transparent;color:#666;cursor:pointer;padding:2px;border-radius:6px}" +
    ".r{display:flex;gap:8px}" +
    ".a{flex:1;height:38px;border-radius:10px;border:1px solid rgba(0,0,0,.12);background:#f5f5f5;color:#111;font-weight:600;font-size:13px;line-height:1;font-family:inherit;cursor:pointer}" +
    ".a.go{background:" + color + ";border-color:transparent;color:#fff}" +
    ".a.end{background:#d93025;border-color:transparent;color:#fff}" +
    ".a[disabled]{opacity:.55;cursor:default}" +
    ".d{display:inline-block;width:8px;height:8px;border-radius:50%;background:#bbb;margin-right:6px;vertical-align:middle}" +
    ".live .d{background:#1e8e3e}" +
    "</style>" +
    '<div class="w"><div class="p" role="dialog" aria-label="' + escapeAttr(label) + '">' +
    '<div class="h"><p class="t"></p><button class="x" type="button" aria-label="Close">' + CLOSE + "</button></div>" +
    '<p class="s" aria-live="polite"><span class="d"></span><span class="st"></span></p>' +
    '<div class="r"><button class="a go" type="button"></button><button class="a mute" type="button" hidden>Mute</button></div>' +
    "</div>" +
    '<button class="b" type="button" aria-expanded="false">' + MIC + "<span></span></button></div>";

  function escapeAttr(v) {
    return String(v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var $ = function (sel) {
    return root.querySelector(sel);
  };
  var wrap = $(".w"),
    panel = $(".p"),
    title = $(".t"),
    statusLine = $(".s"),
    statusText = $(".st"),
    main = $(".a.go"),
    muteBtn = $(".a.mute"),
    launcher = $(".b");
  launcher.querySelector("span").textContent = label;
  title.textContent = label;

  var state = "idle";
  var session = null;
  var pc = null,
    ws = null,
    mic = null,
    audio = null,
    muted = false,
    connected = false,
    reported = false;

  function setState(next, text) {
    state = next;
    statusText.textContent = text || "";
    statusLine.className = "s" + (next === "live" ? " live" : "");
    var busy = next === "starting" || next === "connecting";
    main.disabled = busy;
    main.className = "a " + (next === "live" || busy ? "end" : "go");
    main.textContent = next === "live" || busy ? "End call" : next === "idle" ? "Start call" : "Call again";
    muteBtn.hidden = next !== "live";
  }
  setState("idle", "Talk to us with your microphone.");

  function toggle(open) {
    var isOpen = open === undefined ? !wrap.classList.contains("open") : open;
    wrap.classList.toggle("open", isOpen);
    launcher.setAttribute("aria-expanded", String(isOpen));
    if (isOpen) main.focus();
  }
  launcher.addEventListener("click", function () {
    toggle();
  });
  $(".x").addEventListener("click", function () {
    toggle(false);
    launcher.focus();
  });
  main.addEventListener("click", function () {
    if (state === "live" || state === "connecting" || state === "starting") hangup("You ended the call.");
    else start();
  });
  muteBtn.addEventListener("click", function () {
    muted = !muted;
    if (mic) mic.getAudioTracks().forEach(function (t) {
      t.enabled = !muted;
    });
    muteBtn.textContent = muted ? "Unmute" : "Mute";
  });

  function report() {
    if (!session || reported) return;
    reported = true;
    var body = JSON.stringify({ call_token: session.call_token, connected: connected });
    var url = api + "/sessions/" + encodeURIComponent(session.call_id) + "/end";
    if (navigator.sendBeacon && navigator.sendBeacon(url, body)) return;
    fetch(url, { method: "POST", body: body, keepalive: true, credentials: "omit" }).catch(function () {});
  }

  function cleanup() {
    if (ws) {
      try {
        ws.close();
      } catch (e) {}
    }
    ws = null;
    if (pc) pc.close();
    pc = null;
    if (mic) mic.getTracks().forEach(function (t) {
      t.stop();
    });
    mic = null;
    if (audio) audio.srcObject = null;
    muted = false;
    muteBtn.textContent = "Mute";
  }

  function finish(next, text) {
    if (state === "ended" || state === "failed" || state === "idle") return;
    cleanup();
    report();
    setState(next, text);
  }

  function hangup(text) {
    finish("ended", text);
  }

  function fail(text) {
    finish("failed", text);
  }

  function send(type, payload) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: type, payload: payload }));
  }

  async function start() {
    session = null;
    connected = false;
    reported = false;
    setState("starting", "Allow the microphone to start…");
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
    } catch (e) {
      fail("Microphone access was blocked. Allow it for this site and try again.");
      return;
    }
    setState("connecting", "Connecting…");
    try {
      var res = await fetch(api + "/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        credentials: "omit",
      });
      var data = await res.json().catch(function () {
        return null;
      });
      if (!res.ok) {
        var msg = data && data.error && data.error.message;
        fail(res.status === 429 ? "Busy right now. Try again in a minute." : msg || "The call could not start.");
        return;
      }
      session = data;
      title.textContent = data.agent_name + (data.company_name ? " · " + data.company_name : "");
      await connect(data);
    } catch (e) {
      fail("The call could not start. Check your connection and try again.");
    }
  }

  function connect(s) {
    return new Promise(function (resolve, reject) {
      var pcId = "pc-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      pc = new RTCPeerConnection({
        iceServers: (s.ice_servers || []).map(function (x) {
          var o = { urls: x.urls };
          if (x.username) o.username = x.username;
          if (x.credential) o.credential = x.credential;
          return o;
        }),
      });
      mic.getTracks().forEach(function (t) {
        pc.addTrack(t, mic);
      });
      pc.ontrack = function (e) {
        if (!audio) {
          audio = document.createElement("audio");
          audio.autoplay = true;
          root.appendChild(audio);
        }
        audio.srcObject = e.streams[0] || new MediaStream([e.track]);
      };
      pc.onconnectionstatechange = function () {
        if (!pc) return;
        if (pc.connectionState === "connected") {
          connected = true;
          setState("live", "Connected. Go ahead and talk.");
        } else if (pc.connectionState === "failed") {
          fail("The audio connection could not be established on this network.");
        }
      };
      pc.onicecandidate = function (e) {
        send("ice-candidate", { pc_id: pcId, candidate: e.candidate ? e.candidate.toJSON() : null });
      };
      ws = new WebSocket(s.signaling_url);
      var timer = setTimeout(function () {
        fail("The assistant did not respond.");
        reject(new Error("timeout"));
      }, 10000);
      ws.onopen = async function () {
        clearTimeout(timer);
        try {
          var offer = await pc.createOffer();
          await pc.setLocalDescription(offer);
          send("offer", { sdp: offer.sdp, type: "offer", pc_id: pcId });
          resolve();
        } catch (e) {
          reject(e);
        }
      };
      ws.onerror = function () {
        clearTimeout(timer);
        fail("Could not reach the assistant.");
        resolve();
      };
      ws.onclose = function () {
        if (state === "live" || state === "connecting") hangup("The call ended.");
      };
      ws.onmessage = async function (ev) {
        var msg;
        try {
          msg = JSON.parse(ev.data);
        } catch (e) {
          return;
        }
        if (msg.type === "answer" && msg.payload && msg.payload.sdp && pc) {
          await pc.setRemoteDescription({ type: "answer", sdp: msg.payload.sdp });
        } else if (msg.type === "error") {
          fail((msg.payload && msg.payload.message) || "The assistant reported an error.");
        } else if (msg.type === "call-ended") {
          hangup("The call ended. Thanks for talking with us.");
        }
      };
    });
  }

  window.addEventListener("pagehide", function () {
    if (state === "live" || state === "connecting") {
      cleanup();
      report();
    }
  });

  function mount() {
    document.body.appendChild(host);
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
