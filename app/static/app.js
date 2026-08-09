const API_BASE = '';
let authToken = null;
let currentUser = null;
let currentSessionId = null;
let currentSessionTitle = null;
let ws = null;
let audioContext = null;
let processor = null;
let analyser = null;
let vizRaf = null;
let recording = false;
let timerInt = null;
let timerSecs = 0;
let channels = [];

const statusEl = document.getElementById('listenState');
const timerEl = document.getElementById('timer');
const pulseDot = document.getElementById('pulseDot');
const micIcon = document.getElementById('micIcon');
const newBtn = document.getElementById('newBtn');
const transcriptEl = document.getElementById('transcript');
const memoryListEl = document.getElementById('memoryList');
const answerBoxEl = document.getElementById('answerBox');
const answerTextEl = document.getElementById('answerText');
const answerAudioEl = document.getElementById('answerAudio');
const queryInputEl = document.getElementById('queryInput');
const themeIcon = document.getElementById('themeIcon');
const themeLabel = document.getElementById('themeLabel');
const sourceSel = document.getElementById('sourceSel');
const waveformBars = Array.from(document.querySelectorAll('#waveform .waveform-bar'));

function currentTheme() { return document.documentElement.dataset.theme || 'light'; }

function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem('voxvault-theme', t); } catch (e) {}
    const icon = t === 'dark' ? 'light_mode' : 'dark_mode';
    themeIcon.textContent = icon;
    const authIcon = document.getElementById('authThemeIcon');
    if (authIcon) authIcon.textContent = icon;
    if (themeLabel) themeLabel.textContent = t === 'dark' ? 'Dark Neobrutalism' : 'Neobrutalism (Light)';
}

function toggleTheme() {
    applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
}

function setStatus(text) {
    statusEl.textContent = text;
    statusEl.classList.toggle('active', text === 'Listening' || text === 'Recording');
}

function fmtTime(s) {
    const h = String(Math.floor(s / 3600)).padStart(2, '0');
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const sec = String(s % 60).padStart(2, '0');
    return `${h}:${m}:${sec}`;
}

function startTimer() {
    timerSecs = 0;
    timerEl.textContent = fmtTime(0);
    timerInt = setInterval(() => { timerSecs++; timerEl.textContent = fmtTime(timerSecs); }, 1000);
}
function stopTimer() { if (timerInt) clearInterval(timerInt); timerInt = null; }

function syncRecordingUI() {
    newBtn.disabled = recording;
    pulseDot.style.display = recording ? 'block' : 'none';
    micIcon.textContent = recording ? 'stop' : 'mic';
    if (!recording) resetWaveform();
}

function resetWaveform() {
    if (vizRaf) { cancelAnimationFrame(vizRaf); vizRaf = null; }
    waveformBars.forEach(b => b.style.height = '10%');
}

function startWaveform() {
    if (!analyser) return;
    const bins = new Uint8Array(analyser.frequencyBinCount);
    const smoothing = 0.25;
    const current = waveformBars.map(() => 10);
    function loop() {
        if (!recording || !analyser) { resetWaveform(); return; }
        analyser.getByteFrequencyData(bins);
        const n = waveformBars.length;
        for (let i = 0; i < n; i++) {
            const start = Math.floor(bins.length * i / n);
            const end = Math.floor(bins.length * (i + 1) / n);
            let sum = 0;
            for (let j = start; j < end; j++) sum += bins[j];
            const avg = sum / Math.max(1, end - start);
            const target = Math.max(8, (avg / 255) * 100);
            current[i] = smoothing * current[i] + (1 - smoothing) * target;
            waveformBars[i].style.height = current[i] + '%';
        }
        vizRaf = requestAnimationFrame(loop);
    }
    loop();
}

async function startRecording() {
    if (recording) return;
    if (!currentSessionId) {
        await newSession();
        if (!currentSessionId) return;
    }
    try {
        const mode = (sourceSel && sourceSel.value) || 'mic';
        const wantMic = mode === 'mic' || mode === 'both';
        const wantSys = mode === 'sys' || mode === 'both';
        if (sourceSel) sourceSel.disabled = true;

        audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
        channels = [];

        recording = true;
        syncRecordingUI();
        startTimer();
        setStatus('Recording');

        let micStream = null;
        if (wantMic) {
            micStream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true }
            });
        }

        if (wantSys) {
            // Compatible display-capture: request a tiny video surface too;
            // use only the audio track, then drop video immediately.
            const dispStream = await navigator.mediaDevices.getDisplayMedia({
                video: { width: { ideal: 1 }, height: { ideal: 1 }, frameRate: { ideal: 1 } },
                audio: true
            });
            dispStream.getVideoTracks().forEach(t => t.stop());

            const audioTracks = dispStream.getAudioTracks();
            if (!audioTracks.length) {
                throw new Error('No audio track in the shared surface. In Chrome, tick "Share tab audio" in the picker, or choose "Entire screen" with system audio enabled.');
            }
            attachChannel(dispStream, 'sys');
            sysWatchdog(dispStream);
        }

        if (wantMic) {
            attachChannel(micStream, 'mic');
        }
    } catch (err) {
        console.error('Recording error:', err);
        recording = false;
        stopRecording();
        alert('Could not start audio capture: ' + (err && err.message ? err.message : 'Microphone or system-audio access denied.'));
    }
}

function attachChannel(stream, name) {
    if (!audioContext) return;
    const source = audioContext.createMediaStreamSource(stream);
    const proc = audioContext.createScriptProcessor(4096, 1, 1);

    // Route one channel (or the first mic) into the visualizer analyser.
    if (!analyser) {
        analyser = audioContext.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.6;
        source.connect(analyser);
        startWaveform();
    }

    // Keep the graph running silently to trigger ScriptProcessor buffers.
    const gain = audioContext.createGain();
    gain.gain.value = 0;

    const sock = new WebSocket(`ws://${location.host}/ws/audio/${currentSessionId}?channel=${name}&token=${encodeURIComponent(authToken || '')}`);
    sock.binaryType = 'arraybuffer';
    const entry = { stream, proc, sock, name, source };
    channels.push(entry);

    sock.onopen = () => {
        proc.onaudioprocess = (e) => {
            const chunk = new Float32Array(e.inputBuffer.getChannelData(0));
            if (sock.readyState === WebSocket.OPEN) sock.send(chunk.buffer);
        };
        source.connect(proc);
        proc.connect(gain);
        gain.connect(audioContext.destination);
    };
    sock.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'transcription') {
            addTranscript(msg.text, msg.confidence, (msg.speaker || name));
            loadMemory();
        } else if (msg.type === 'memory_command') {
            addCommandChip(msg);
        }
    };
    sock.onclose = () => { if (recording) stopRecording(); };
    sock.onerror = () => setStatus('Connection Error');
}

function sysWatchdog(stream) {
    // Monitor the captured system surface's actual audio signal.
    if (!audioContext) return;
    const src = audioContext.createMediaStreamSource(stream);
    const ana = audioContext.createAnalyser();
    ana.fftSize = 512;
    const tapGain = audioContext.createGain();
    tapGain.gain.value = 0; // silent tap, no audible output
    src.connect(ana);
    ana.connect(tapGain);
    tapGain.connect(audioContext.destination); // pulled through the graph so it actually samples
    const buf = new Float32Array(ana.fftSize);
    const started = Date.now();
    let warmed = 0;
    const iv = setInterval(() => {
        if (!recording) {
            clearInterval(iv);
            try { src.disconnect(); ana.disconnect(); tapGain.disconnect(); } catch (e) {}
            return;
        }
        ana.getFloatTimeDomainData(buf);
        let rms = 0;
        for (let i = 0; i < buf.length; i++) rms += buf[i] * buf[i];
        rms = Math.sqrt(rms / buf.length);
        if (rms >= 0.004) warmed = Date.now();
        // Only warn after 5s of recording AND 4s of continuous silence.
        if (Date.now() - started > 5000 && (Date.now() - warmed) > 4000) {
            setStatus('No system audio detected');
            showSysHint();
            clearInterval(iv);
            try { src.disconnect(); ana.disconnect(); tapGain.disconnect(); } catch (e) {}
        }
    }, 400);
}

function showSysHint() {
    const existing = document.getElementById('sysHint');
    if (existing) return;
    const hint = document.createElement('div');
    hint.id = 'sysHint';
    hint.className = 'error-note';
    hint.innerHTML = '<b>No system audio is arriving.</b> If you are capturing a browser tab, Chrome needs <b>"Share tab audio"</b> ticked in the picker. If capturing the screen, enable <b>system audio</b>. If the video is muted, unmute it. Then click End Session and record again.';
    const feed = document.querySelector('.feed-inner');
    if (feed) feed.insertBefore(hint, feed.firstChild);
}

function stopRecording() {
    recording = false;
    if (vizRaf) { cancelAnimationFrame(vizRaf); vizRaf = null; }
    resetWaveform();
    channels.forEach(c => {
        try { if (c.proc) c.proc.disconnect(); } catch (e) {}
        try { if (c.sock) c.sock.close(); } catch (e) {}
        try { c.stream && c.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
    });
    channels = [];
    if (analyser) { analyser.disconnect(); analyser = null; }
    if (audioContext) { try { audioContext.close(); } catch (e) {} audioContext = null; }
    stopTimer();
    setStatus('Idle');
    syncRecordingUI();
    if (sourceSel) sourceSel.disabled = false;
}

function toggleRecording() { if (recording) stopRecording(); else startRecording(); }

function addTranscript(text, confidence, speaker) {
    const div = document.createElement('div');
    div.className = 'transcript-card live';
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const sp = speaker || 'You';
    const spClass = {
        'You': 'sp-you',
        'Speaker A': 'sp-a', 'Speaker B': 'sp-b',
        'Speaker C': 'sp-c', 'Speaker D': 'sp-d'
    }[sp] || 'sp-you';
    div.innerHTML = `
        <div class="row">
            <span class="meta"><span class="sp-chip ${spClass}">${sp}</span> <span class="live-badge">Live</span></span>
            <span class="meta">${time}</span>
        </div>
        <p>${text}</p>
        <div class="transcribing"><div class="live-cursor"></div>Transcribing...</div>
        <div class="acc">${Math.round(confidence * 100)}% accuracy</div>`;
    const first = transcriptEl.firstChild;
    if (first) transcriptEl.insertBefore(div, first); else transcriptEl.appendChild(div);
}

async function loadMemory() {
    try {
        const params = currentSessionId ? `?session_id=${encodeURIComponent(currentSessionId)}` : '';
        const res = await fetch(API_BASE + '/api/memory' + params, {
            headers: authHeaders()
        });
        if (res.status === 401) { handleUnauthorized(); return; }
        const data = await res.json();
        memoryListEl.innerHTML = '';
        if (!data.segments.length) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            empty.innerHTML = 'No memories yet.<br>Start a recording to build the meeting memory.';
            memoryListEl.appendChild(empty);
            return;
        }
        data.segments.slice().reverse().forEach(seg => {
            const div = document.createElement('div');
            div.className = 'memory-card';
            div.onclick = () => askQuestion(seg.text);
            const time = new Date(seg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const typeBadge = (seg.memory_type && seg.memory_type !== 'transcript')
                ? `<span class="mem-type">${seg.memory_type}${seg.status === 'open' ? ' · open' : ''}</span>` : '';
            const playBtn = seg.audio_url
                ? `<span class="mem-play material-symbols-outlined" title="Replay audio" data-audio>play_arrow</span>` : '';
            div.innerHTML = `<div class="text">${seg.text}</div>${playBtn}<div class="meta">${typeBadge}<span>${seg.speaker}</span><span>${time}</span></div>`;
            const pb = div.querySelector('[data-audio]');
            if (pb) {
                pb.addEventListener('click', (ev) => {
                    ev.stopPropagation();
                    playAudioUrl(seg.audio_url);
                });
            }
            memoryListEl.appendChild(div);
        });
    } catch (e) {
        console.error('Load memory failed:', e);
    }
}

function askQuestion(q) {
    showView('dashboard');
    queryInputEl.value = q || '';
    submitQuery();
}

function submitQuery() {
    const q = queryInputEl.value.trim();
    if (!q) return;
    if (!currentSessionId) { answerTextEl.textContent = 'No active session yet. Click New Session to start one.'; answerBoxEl.style.display = 'block'; return; }
    answerBoxEl.style.display = 'block';
    answerTextEl.textContent = 'Thinking...';
    answerAudioEl.style.display = 'none';
    fetch(API_BASE + '/api/query', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
        body: JSON.stringify({ query: q, top_k: 5, session_id: currentSessionId })
    }).then(async r => {
        if (r.status === 401) { handleUnauthorized(); throw new Error('unauth'); }
        return r.json();
    }).then(data => {
        answerTextEl.textContent = data.answer;
        if (data.audio_url) {
            answerAudioEl.src = data.audio_url;
            answerAudioEl.style.display = 'block';
            answerAudioEl.play().catch(() => {});
        }
    }).catch(e => {
        answerTextEl.textContent = 'Error querying memory';
        console.error(e);
    });
}

function showView(name) {
    document.querySelectorAll('.nav-item').forEach(n => {
        n.classList.toggle('active', n.dataset.view === name);
    });
    document.querySelectorAll('.view').forEach(v => {
        v.classList.toggle('active', v.id === 'view-' + name);
    });
    if (name === 'meetings') loadMeetings();
    if (name === 'workspace') loadSystem();
}

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        showView(item.dataset.view);
    });
});

async function loadMeetings() {
    const list = document.getElementById('meetingsList');
    try {
        const res = await fetch(API_BASE + '/api/sessions', { headers: authHeaders() });
        if (res.status === 401) { handleUnauthorized(); return; }
        const sessions = await res.json();
        list.innerHTML = '';
        if (!sessions.length) {
            list.innerHTML = '<div class="empty-state">No sessions yet.<br>Click "New Session" on the Dashboard tab to record your first meeting.</div>';
            return;
        }
        sessions.forEach(sess => {
            const card = document.createElement('div');
            card.className = 'meet-card';
            const time = new Date(sess.updated_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
            card.innerHTML = `
                <div class="h">
                    <span class="t">${sess.title}</span>
                    <span class="m">${sess.segment_count} segments</span>
                    <div class="card-actions">
                        <button class="mini-btn" data-action="rename" title="Rename session">Rename</button>
                        <button class="mini-btn" data-action="delete" title="Delete session">Delete</button>
                    </div>
                </div>
                <div class="s"><span class="meet-chip clickable" data-open="1">Open session</span></div>
                <div style="font-size:0.8rem; color:var(--on-surface-variant); margin-top:0.5rem; font-family:var(--font-mono);">${time}</div>`;
            card.querySelector('[data-open]').addEventListener('click', () => openSession(sess.session_id, sess.title));
            card.querySelector('[data-action="delete"]').addEventListener('click', async () => {
                if (!confirm(`Delete session "${sess.title}" and all its segments?`)) return;
                try {
                    const del = await fetch(API_BASE + `/api/sessions/${sess.session_id}`, { method: 'DELETE', headers: authHeaders() });
                    if (del.status === 401) { handleUnauthorized(); return; }
                } catch (e) {}
                loadMeetings();
            });
            card.querySelector('[data-action="rename"]').addEventListener('click', async () => {
                const newTitle = prompt('Rename session:', sess.title);
                if (newTitle === null || !newTitle.trim()) return;
                try {
                    const ren = await fetch(API_BASE + `/api/sessions/${sess.session_id}`, {
                        method: 'PATCH',
                        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
                        body: JSON.stringify({ title: newTitle.trim() })
                    });
                    if (ren.status === 401) { handleUnauthorized(); return; }
                } catch (e) {}
                loadMeetings();
                if (currentSessionId === sess.session_id) {
                    setSession(sess.session_id, newTitle.trim());
                }
            });
            list.appendChild(card);
        });
    } catch (e) {
        list.innerHTML = '<div class="empty-state">Failed to load meetings.</div>';
        console.error(e);
    }
}

async function loadSystem() {
    try {
        const res = await fetch(API_BASE + '/api/system', { headers: authHeaders() });
        if (res.status === 401) { handleUnauthorized(); return; }
        const sys = await res.json();
        document.getElementById('statMemories').textContent = sys.vector.vectors;
        document.getElementById('statStt').textContent = sys.stt.model;
        document.getElementById('statTts').textContent = sys.tts.has_api_key ? 'Rime coda' : 'mock (no key)';
        document.getElementById('statVec').textContent = sys.vector.available ? 'Online' : 'Offline';
        document.getElementById('vecDot').className = 'badge-dot ' + (sys.vector.available ? 'dot-on' : 'dot-off');
        document.getElementById('vecName').textContent = sys.vector.collection;
    } catch (e) {
        console.error('System load failed:', e);
    }
}

async function clearMemory() {
    if (!confirm('Clear ALL stored meeting memories for this account? This cannot be undone.')) return;
    try {
        await fetch(API_BASE + '/api/memory', { method: 'DELETE', headers: authHeaders() });
        loadMemory();
        loadMeetings();
        loadSystem();
        alert('All memories cleared.');
    } catch (e) {
        alert('Failed to clear memory.');
    }
}

function showToast(msg, type = 'error') {
    const old = document.getElementById('toast');
    if (old) old.remove();
    const toast = document.createElement('div');
    toast.className = 'toast' + (type ? ' ' + type : '');
    toast.id = 'toast';
    const icon = document.createElement('span');
    icon.className = 'material-symbols-outlined';
    icon.textContent = type === 'success' ? 'check_circle' : 'error';
    const text = document.createElement('span');
    text.textContent = msg;
    const close = document.createElement('button');
    close.className = 'toast-close';
    close.type = 'button';
    close.textContent = '✕';
    close.onclick = () => toast.remove();
    toast.append(icon, text, close);
    document.body.appendChild(toast);
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.remove(), 4000);
}

function playAudioUrl(url) {
    const a = new Audio(url);
    a.play().catch(e => console.error('Replay audio failed:', e));
}

function addCommandChip(msg) {
    const div = document.createElement('div');
    div.className = 'cmd-chip';
    const icon = { remember: 'check_circle', action: 'task_alt', done: 'done_all', forget: 'delete' }[msg.action] || 'info';
    div.innerHTML = `<span class="material-symbols-outlined">${icon}</span><span><b>VoxVault</b> ${msg.confirm || ''}</span>`;
    const first = transcriptEl.firstChild;
    if (first) transcriptEl.insertBefore(div, first); else transcriptEl.appendChild(div);
    loadOpenActions();
    loadMemory();
}

async function loadOpenActions() {
    try {
        const res = await fetch(API_BASE + '/api/actions/open', { headers: authHeaders() });
        if (res.status === 401) { handleUnauthorized(); return; }
        const actions = await res.json();
        const existing = document.getElementById('openActionsBanner');
        if (existing) existing.remove();
        if (!actions.length) return;
        const banner = document.createElement('div');
        banner.id = 'openActionsBanner';
        banner.className = 'actions-banner';
        const n = actions.length;
        const chips = actions.slice(0, 3).map(a => a.text).join(' | ');
        banner.innerHTML = `
            <div class="actions-banner-head">
                <span class="material-symbols-outlined">fact_check</span>
                <b>${n} open action item${n === 1 ? '' : 's'} waiting from past sessions</b>
                <button class="chip" onclick="askQuestion('action items')">Ask VoxVault</button>
            </div>
            <div class="actions-banner-body">${chips}</div>`;
        const feed = document.querySelector('.feed-inner');
        if (feed) feed.insertBefore(banner, feed.firstChild);
    } catch (e) { console.error('loadOpenActions failed:', e); }
}

// ---------------- auth ----------------
const authOverlayEl = document.getElementById('authOverlay');
const authTitleEl = document.getElementById('authTitle');
const authSubEl = document.getElementById('authSub');
const authBtnEl = document.getElementById('authBtn');
const authSwitchEl = document.getElementById('authSwitch');
const authSwitchLinkEl = document.getElementById('authSwitchLink');
const authModeEl = document.getElementById('authMode');
const authEmailEl = document.getElementById('authEmail');
const authPasswordEl = document.getElementById('authPassword');
let authMode = 'login';
const SUPABASE_ENABLED = !!window.APP_CONFIG && !!window.APP_CONFIG.supabaseEnabled;

function authHeaders() {
    return authToken ? { 'Authorization': 'Bearer ' + authToken } : {};
}

function handleUnauthorized() {
    logout();
}

function setAuthMode(mode) {
    authMode = mode;
    if (mode === 'login') {
        authTitleEl.textContent = 'Welcome back';
        authSubEl.textContent = 'Sign in to your VoxVault workspace. Your meeting memory is saved per account.';
        authBtnEl.textContent = 'Sign In';
        authSwitchEl.innerHTML = 'New here? <a onclick="toggleAuthMode()" id="authSwitchLink">Create an account</a>';
    } else {
        authTitleEl.textContent = 'Create your account';
        authSubEl.textContent = 'Your transcripts and meeting memory are stored under this account.';
        authBtnEl.textContent = 'Create Account';
        authSwitchEl.innerHTML = 'Already a member? <a onclick="toggleAuthMode()" id="authSwitchLink">Sign in</a>';
    }
    const err = authOverlayEl.querySelector('.error-note');
    if (err) err.remove();
    if (authModeEl) {
        authModeEl.textContent = SUPABASE_ENABLED ? 'Credentials: Supabase Auth' : 'Credentials: local dev fallback (no Supabase configured)';
    }
}

function toggleAuthMode() {
    setAuthMode(authMode === 'login' ? 'signup' : 'login');
}

function showAuthError(msg) {
    showToast(msg);
}

async function submitAuth() {
    const email = authEmailEl.value.trim();
    const password = authPasswordEl.value;
    if (!email || !password) { showToast('Enter both email and password.'); return; }
    if (!/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(email)) {
        showToast('Enter a valid email address.');
        return;
    }
    if (password.length < 6) { showToast('Password must be at least 6 characters.'); return; }
    btnBusy(authBtnEl, true);
    try {
        const res = await fetch(API_BASE + `/api/auth/${authMode}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            let msg = body.detail;
            if (Array.isArray(msg)) msg = (msg[0] && msg[0].msg) || 'Invalid input.';
            showToast(msg || (authMode === 'signup' ? 'Signup failed.' : 'Invalid credentials.'));
            return;
        }
        const data = await res.json();
        authToken = data.token;
        currentUser = data.user;
        try { localStorage.setItem('voxvault_token', data.token); localStorage.setItem('voxvault_user', JSON.stringify(data.user)); } catch (e) {}
        enterApp();
        const saved = localStorage.getItem('voxvault_session');
        if (saved) openSession(saved, 'Session');
        else newSession();
    } catch (e) {
        showToast('Network error. Is the server running?');
    } finally {
        btnBusy(authBtnEl, false);
    }
}

function btnBusy(btn, busy) {
    if (busy) { btn.disabled = true; btn.textContent = 'Please wait...'; }
    else { btn.disabled = false; setAuthMode(authMode); }
}

function logout() {
    authToken = null;
    currentUser = null;
    currentSessionId = null;
    currentSessionTitle = null;
    try { localStorage.removeItem('voxvault_token'); localStorage.removeItem('voxvault_user'); localStorage.removeItem('voxvault_session'); } catch (e) {}
    if (recording) stopRecording();
    showAuth();
}

function showAuth() {
    authOverlayEl.classList.remove('hidden');
    authBtnEl.disabled = false;
    setAuthMode(authMode === 'signup' ? 'signup' : 'login');
}

function enterApp() {
    authOverlayEl.classList.add('hidden');
    const email = (currentUser && currentUser.email) || 'user';
    document.getElementById('avatarLabel').textContent = (email[0] || 'U').toUpperCase();
    document.getElementById('profileName').textContent = (email.split('@')[0] || 'You');
    document.getElementById('profileEmail').textContent = email;
}

// ---------------- sessions ----------------
async function newSession(autoRecord) {
    if (!authToken) { showAuth(); return; }
    if (recording) stopRecording();
    try {
        const res = await fetch(API_BASE + '/api/sessions', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
            body: JSON.stringify({})
        });
        if (res.status === 401) { handleUnauthorized(); return; }
        const sess = await res.json();
        setSession(sess.session_id, sess.title);
        try { localStorage.setItem('voxvault_session', sess.session_id); } catch (e) {}
        transcriptEl.innerHTML = '';
        memoryListEl.innerHTML = '';
        loadMemory();
        loadMeetings();
        loadOpenActions();
        if (autoRecord) startRecording();
    } catch (e) {
        console.error('newSession failed:', e);
    }
}

async function openSession(sessionId, title) {
    if (recording) stopRecording();
    if (!title) {
        try {
            const res = await fetch(API_BASE + `/api/sessions/${sessionId}`, { headers: authHeaders() });
            if (res.ok) {
                const s = await res.json();
                title = s.title;
            }
        } catch (e) {}
    }
    setSession(sessionId, title);
    try { localStorage.setItem('voxvault_session', sessionId); } catch (e) {}
    showView('dashboard');
    transcriptEl.innerHTML = '';
    memoryListEl.innerHTML = '';
    answerBoxEl.style.display = 'none';
    loadOpenActions();
    await loadMemory();
    await loadTranscript();
}

async function loadTranscript() {
    try {
        if (!currentSessionId) return;
        const res = await fetch(API_BASE + `/api/memory?session_id=${encodeURIComponent(currentSessionId)}`, {
            headers: authHeaders()
        });
        if (res.status === 401) { handleUnauthorized(); return; }
        const data = await res.json();
        transcriptEl.innerHTML = '';
        if (!data.segments.length) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            empty.innerHTML = 'This session has no transcript yet.<br>Start a recording to add content.';
            transcriptEl.appendChild(empty);
            return;
        }
        // oldest -> newest (feed reads top to bottom)
        data.segments.slice().sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp)).forEach(seg => {
            const div = document.createElement('div');
            div.className = 'transcript-card';
            const time = new Date(seg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const sp = seg.speaker || 'You';
            const spClass = { 'You': 'sp-you', 'Speaker A': 'sp-a', 'Speaker B': 'sp-b', 'Speaker C': 'sp-c', 'Speaker D': 'sp-d' }[sp] || 'sp-you';
            div.innerHTML = `
                <div class="row">
                    <span class="meta"><span class="sp-chip ${spClass}">${sp}</span></span>
                    <span class="meta">${time}</span>
                </div>
                <p>${seg.text}</p>`;
            transcriptEl.appendChild(div);
        });
    } catch (e) {
        console.error('loadTranscript failed:', e);
    }
}

function setSession(sessionId, title) {
    currentSessionId = sessionId;
    currentSessionTitle = title || 'Untitled session';
    document.getElementById('sessionName').textContent = currentSessionTitle;
}

applyTheme(currentTheme());
timerEl.textContent = fmtTime(0);
syncRecordingUI();

// Boot: render auth screen unless a valid token exists.
(async function boot() {
    setAuthMode(authMode);
    try {
        const t = localStorage.getItem('voxvault_token');
        if (t) {
            authToken = t;
            const res = await fetch(API_BASE + '/api/auth/me', { headers: authHeaders() });
            if (res.ok) {
                const data = await res.json();
                currentUser = data.user;
                enterApp();
                const saved = localStorage.getItem('voxvault_session');
                if (saved) openSession(saved, 'Session');
                else newSession();
                return;
            }
            authToken = null;
        }
    } catch (e) { authToken = null; }
    showAuth();
})();