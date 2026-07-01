/*
 * Brain Orb voice tile — client-held Gemini Live (#58 Phase 3, trinity-enterprise#60).
 *
 * Adapted from the standalone Cornelius voice page, hardened for Trinity:
 *   - NO hardcoded API key. The browser authenticates its Gemini Live socket with
 *     a short-lived EPHEMERAL token minted by the Trinity backend. We never see the
 *     platform Gemini key, and never see the user's JWT — the PARENT orb page holds
 *     the JWT and mints the token on our behalf (relayed over postMessage).
 *   - NO tool declarations here. The whole Live config (model, voice, system prompt,
 *     tool surface) is LOCKED into the ephemeral token server-side; the browser only
 *     sends {setup:{model}} (matches the google-genai SDK's Constrained path). This
 *     is also what keeps the deferred Phase-4 WRITE tools off — the browser cannot
 *     widen the tool surface.
 *   - NO p5 CDN visualiser (CSP script-src 'self'), NO localhost tool proxy, NO
 *     transcript logging (Phase 4). Every tool call is forwarded to the parent orb,
 *     which runs it locally (ORB_TOOLS) and drives the visualisation.
 *
 * Ephemeral-token wire format (verified against the SDK's live.py Constrained path):
 *   wss://.../v1alpha.GenerativeService.BidiGenerateContentConstrained?access_token=<token>
 *   The SDK sets `Authorization: Token <name>` as a header; a browser WebSocket
 *   cannot set headers, so the token rides as the `access_token` query param. The
 *   exact live handshake is the one part only verifiable against the live API.
 */
(function () {
  'use strict';

  var WSS_BASE = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained';
  var MIC_RATE = 16000;
  var OUT_RATE = 24000;
  var TOKEN_TIMEOUT_MS = 10000;   // how long we wait for the parent to relay a token
  var ORB_TOOL_TIMEOUT_MS = 8000; // scope re-export can be slow; visual tools are instant

  // ── state ──────────────────────────────────────────────────────────────────
  var appState = 'IDLE';
  var ws = null;
  var wsClosedByUs = false;
  var muteOutput = false;
  var model = null;               // supplied by the parent along with the token

  var audioCtx = null, micStream = null, micNode = null, nextPlayTime = 0;

  function $(id) { return document.getElementById(id); }

  // ── parent bridge (the orb page holds the JWT + runs the visual tools) ───────
  // The parent replies to a token request with {type:'orb-voice-token', ...}. We
  // key each request so a stale reply can't resolve a newer request.
  var _tokenWaiter = null;
  function requestToken() {
    return new Promise(function (resolve, reject) {
      if (window.parent === window) { reject(new Error('not embedded in the orb')); return; }
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return; settled = true; _tokenWaiter = null;
        reject(new Error('voice token timed out'));
      }, TOKEN_TIMEOUT_MS);
      _tokenWaiter = function (msg) {
        if (settled) return; settled = true; clearTimeout(timer); _tokenWaiter = null;
        if (msg && msg.ok && msg.token) resolve(msg);
        else reject(new Error((msg && msg.error) || 'could not start voice'));
      };
      try { window.parent.postMessage({ type: 'orb-voice-token-request' }, '*'); }
      catch (e) { clearTimeout(timer); _tokenWaiter = null; reject(e); }
    });
  }

  // Forward a Gemini tool call to the parent orb, which runs it against ORB_TOOLS
  // and returns the result (drives the visualisation locally — no server hop).
  function callParentOrb(name, args) {
    return new Promise(function (resolve) {
      if (window.parent === window) { resolve({ error: 'not embedded in the orb' }); return; }
      var id = 'g' + Math.random().toString(36).slice(2);
      var done = false;
      var h = function (ev) {
        if (ev.data && ev.data.type === 'orb-tool-result' && ev.data.id === id) {
          if (done) return; done = true;
          window.removeEventListener('message', h); resolve(ev.data.output);
        }
      };
      window.addEventListener('message', h);
      window.parent.postMessage({ type: 'orb-tool', id: id, name: name, args: args }, '*');
      setTimeout(function () {
        if (done) return; done = true;
        window.removeEventListener('message', h); resolve({ error: 'orb did not respond' });
      }, ORB_TOOL_TIMEOUT_MS);
    });
  }

  window.addEventListener('message', function (ev) {
    // Same-origin only: the token relay carries the ephemeral credential, so never
    // trust a message from another origin (defense-in-depth atop frame-ancestors 'self').
    if (ev.origin !== window.location.origin) return;
    var m = ev.data;
    if (!m || typeof m !== 'object') return;
    // Parent (orb.js) tells us to fully disconnect (voice tile toggled off with V).
    if (m.type === 'orb-voice-stop') { try { endConversation(); } catch (e) {} return; }
    // Token relay reply.
    if (m.type === 'orb-voice-token' && _tokenWaiter) { _tokenWaiter(m); return; }
  });

  // ── UI ───────────────────────────────────────────────────────────────────────
  function setState(s) {
    appState = s;
    var dot = $('state-dot'), txt = $('status-text');
    var startBtn = $('start-btn'), endBtn = $('end-btn'), muteBtn = $('mute-btn');
    dot.className = s.toLowerCase();
    switch (s) {
      case 'IDLE':
        txt.textContent = 'press start to speak';
        startBtn.style.display = 'inline-block'; startBtn.disabled = false;
        endBtn.style.display = 'none'; muteBtn.style.display = 'none';
        break;
      case 'CONNECTING':
        txt.textContent = 'connecting…';
        startBtn.style.display = 'none';
        endBtn.style.display = 'inline-block'; muteBtn.style.display = 'inline-block';
        break;
      case 'READY':
        txt.textContent = muteOutput ? 'listening — replies muted' : 'listening — speak freely';
        startBtn.style.display = 'none';
        endBtn.style.display = 'inline-block'; muteBtn.style.display = 'inline-block';
        break;
      case 'SPEAKING':
        txt.textContent = muteOutput ? 'replying (muted)…' : 'speaking…';
        break;
      case 'ERROR':
        startBtn.style.display = 'inline-block'; startBtn.disabled = false;
        endBtn.style.display = 'none'; muteBtn.style.display = 'none';
        break;
    }
  }

  function setError(msg) {
    console.warn('[brain-orb voice]', msg);
    $('status-text').textContent = msg;
    appState = 'ERROR';
    $('state-dot').className = 'error';
    $('start-btn').style.display = 'inline-block'; $('start-btn').disabled = false;
    $('end-btn').style.display = 'none'; $('mute-btn').style.display = 'none';
  }

  function toggleMute() {
    muteOutput = !muteOutput;
    var btn = $('mute-btn');
    btn.classList.toggle('active', muteOutput);
    btn.textContent = muteOutput ? 'Resume Replies' : "Don't Interrupt";
    if (audioCtx) nextPlayTime = audioCtx.currentTime;   // don't replay queued audio
    setState(appState);
  }

  // ── conversation lifecycle ─────────────────────────────────────────────────
  async function startConversation() {
    setState('CONNECTING');
    wsClosedByUs = false; nextPlayTime = 0;

    var tokenInfo;
    try {
      tokenInfo = await requestToken();   // parent mints via the JWT-gated broker
    } catch (e) { setError('voice unavailable: ' + (e.message || e)); return; }
    model = tokenInfo.model;

    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
    } catch (e) {
      cleanupAudio();
      setError('allow microphone access to talk');
      return;
    }

    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') await audioCtx.resume();
      var micSource = audioCtx.createMediaStreamSource(micStream);
      micNode = await setupMicCapture(micSource);

      // access_token in the URL is the ephemeral (single-use, model-locked, short-
      // TTL) token — safe to expose to the browser by design; the JWT never is.
      ws = new WebSocket(WSS_BASE + '?access_token=' + encodeURIComponent(tokenInfo.token));

      ws.onopen = function () {
        // Config is locked in the token; send only the model (SDK Constrained path).
        ws.send(JSON.stringify({ setup: { model: model } }));
        setTimeout(function () {
          if (appState === 'CONNECTING') {
            setError('timed out connecting to voice');
            wsClosedByUs = true; try { ws && ws.close(); } catch (e) {} ws = null;
            cleanupAudio();
          }
        }, 8000);
      };
      ws.onmessage = async function (event) {
        try {
          var text;
          if (typeof event.data === 'string') text = event.data;
          else if (event.data instanceof ArrayBuffer) text = new TextDecoder().decode(event.data);
          else if (event.data instanceof Blob) text = await event.data.text();
          else return;
          await handleServerMessage(JSON.parse(text));
        } catch (e) { console.warn('[brain-orb voice] parse error:', e.message); }
      };
      ws.onerror = function () { console.warn('[brain-orb voice] WS error (close follows)'); };
      ws.onclose = function (event) {
        if (wsClosedByUs) return;
        cleanupAudio();
        if (event.code === 1000 || event.code === 1001) setState('IDLE');
        else setError('voice disconnected (' + event.code + ') — press start to retry');
      };
    } catch (err) {
      cleanupAudio();
      setError('voice error: ' + (err.message || err));
    }
  }

  async function handleServerMessage(data) {
    if (data.setupComplete !== undefined || data.setup_complete !== undefined) {
      setState('READY');
      return;
    }

    var toolCall = data.toolCall || data.tool_call;
    if (toolCall) {
      var calls = toolCall.functionCalls || toolCall.function_calls || [];
      var responses = await Promise.all(calls.map(async function (fc) {
        // Every tool is an orb tool (the locked manifest only declares orb tools);
        // the parent runs it against ORB_TOOLS and drives the visualisation.
        var output = await callParentOrb(fc.name, fc.args || {});
        return { id: fc.id, response: { output: output } };
      }));
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ tool_response: { function_responses: responses } }));
      }
      return;
    }

    var content = data.serverContent || data.server_content;
    if (content) {
      var turn = content.modelTurn || content.model_turn;
      if (turn && turn.parts) {
        setState('SPEAKING');
        for (var i = 0; i < turn.parts.length; i++) {
          var blob = turn.parts[i].inlineData || turn.parts[i].inline_data;
          if (blob && blob.data) enqueueAudio(blob.data);
        }
      }
      if (content.turnComplete || content.turn_complete ||
          content.generationComplete || content.generation_complete) {
        var wait = Math.max(0, (nextPlayTime - audioCtx.currentTime) * 1000 + 150);
        setTimeout(function () { if (appState === 'SPEAKING') setState('READY'); }, wait);
      }
    }
  }

  function endConversation() {
    wsClosedByUs = true;
    if (ws) { try { ws.close(1000, 'user ended'); } catch (e) {} ws = null; }
    cleanupAudio();
    if (muteOutput) {
      muteOutput = false;
      var mb = $('mute-btn'); if (mb) { mb.classList.remove('active'); mb.textContent = "Don't Interrupt"; }
    }
    setState('IDLE');
  }

  // ── audio ──────────────────────────────────────────────────────────────────
  async function setupMicCapture(micSource) {
    try {
      // Same-origin worklet file (not a blob: URL) → passes CSP script-src 'self'.
      await audioCtx.audioWorklet.addModule('./mic-worklet.js');
      var node = new AudioWorkletNode(audioCtx, 'mic-capture');
      micSource.connect(node);
      node.port.onmessage = function (e) { sendAudioChunk(e.data); };
      return node;
    } catch (e) {
      // Fallback for browsers/policies where the worklet won't load.
      console.warn('[brain-orb voice] AudioWorklet failed, using ScriptProcessor:', e.message);
      var sp = audioCtx.createScriptProcessor(4096, 1, 1);
      var sink = audioCtx.createGain(); sink.gain.value = 0;
      micSource.connect(sp); sp.connect(sink); sink.connect(audioCtx.destination);
      sp.onaudioprocess = function (ev) { sendAudioChunk(ev.inputBuffer.getChannelData(0).slice()); };
      return sp;
    }
  }

  function sendAudioChunk(float32Data) {
    if (!ws || ws.readyState !== WebSocket.OPEN || appState === 'CONNECTING') return;
    var down = downsample(float32Data, audioCtx.sampleRate, MIC_RATE);
    var pcm16 = floatToInt16(down);
    ws.send(JSON.stringify({
      realtimeInput: { audio: { mimeType: 'audio/pcm;rate=' + MIC_RATE, data: arrayBufferToBase64(pcm16.buffer) } }
    }));
  }

  function enqueueAudio(base64Data) {
    if (muteOutput) { nextPlayTime = audioCtx.currentTime; return; }
    var binary = atob(base64Data);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    var int16 = new Int16Array(bytes.buffer);
    var f32 = new Float32Array(int16.length);
    for (var j = 0; j < int16.length; j++) f32[j] = int16[j] / 32768.0;
    var buf = audioCtx.createBuffer(1, f32.length, OUT_RATE);
    buf.copyToChannel(f32, 0);
    var src = audioCtx.createBufferSource();
    src.buffer = buf; src.connect(audioCtx.destination);
    var at = Math.max(audioCtx.currentTime + 0.01, nextPlayTime);
    src.start(at);
    nextPlayTime = at + buf.duration;
  }

  function cleanupAudio() {
    if (micNode) { try { micNode.disconnect(); } catch (e) {} micNode = null; }
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
  }

  function downsample(buf, fromRate, toRate) {
    if (fromRate === toRate) return buf;
    var ratio = fromRate / toRate;
    var out = new Float32Array(Math.round(buf.length / ratio));
    for (var i = 0; i < out.length; i++) {
      var s = Math.floor(i * ratio), e = Math.min(Math.ceil((i + 1) * ratio), buf.length), sum = 0;
      for (var j = s; j < e; j++) sum += buf[j];
      out[i] = sum / (e - s);
    }
    return out;
  }

  function floatToInt16(f32) {
    var i16 = new Int16Array(f32.length);
    for (var i = 0; i < f32.length; i++) {
      var s = Math.max(-1, Math.min(1, f32[i]));
      i16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return i16;
  }

  function arrayBufferToBase64(buf) {
    var bytes = new Uint8Array(buf), b = '';
    for (var i = 0; i < bytes.byteLength; i++) b += String.fromCharCode(bytes[i]);
    return btoa(b);
  }

  // ── init ───────────────────────────────────────────────────────────────────
  function init() {
    $('start-btn').onclick = startConversation;
    $('end-btn').onclick = endConversation;
    $('mute-btn').onclick = toggleMute;
    setState('IDLE');
    // Tell the parent we're loaded (it un-hides / positions the tile as needed).
    if (window.parent !== window) {
      try { window.parent.postMessage({ type: 'orb-voice-ready' }, '*'); } catch (e) {}
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
