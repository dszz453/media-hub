/* ==========================================================================
   MediaHub 前端
   纯原生 JS，无构建步骤 —— 让 Docker 镜像保持简单。
   ========================================================================== */
(() => {
  'use strict';

  // ======================================================================
  // 状态
  // ======================================================================
  const S = {
    view: 'search',
    platform: 'all',
    keyword: '',
    results: [],          // 搜索结果
    searching: false,
    queue: [],            // 播放队列 [{track}]
    cursor: -1,           // 当前播放位置
    current: null,        // 当前曲目
    mode: localStorage.getItem('mh.mode') || 'list', // list | single | shuffle
    volume: parseFloat(localStorage.getItem('mh.volume') || '0.8'),
    muted: false,
    favs: new Set(),      // 收藏的 track_id
    history: [],
    playlists: [],
    openPlaylist: null,
    jobs: {},             // trackId -> {status, progress, error}
    polling: false,
    parsedItem: null,
  };

  const audio = document.getElementById('audio');
  const $ = (sel) => document.querySelector(sel);

  // ======================================================================
  // 工具
  // ======================================================================
  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  }

  function fmtSize(bytes) {
    let v = Number(bytes) || 0;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + ' ' + units[i];
  }

  function toast(message, type = 'info', ms = 2600) {
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.textContent = message;
    $('#toastWrap').appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity .25s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 260);
    }, ms);
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    let body = null;
    try { body = await res.json(); } catch (_) { /* 可能无 body */ }
    if (!res.ok) {
      const detail = (body && (body.detail || body.message)) || ('HTTP ' + res.status);
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return body;
  }

  const PLATFORM_LABEL = { xhs: '小红书', douyin: '抖音', youtube: 'YouTube' };
  const PLATFORM_ICON = { xhs: '📕', douyin: '🎵', youtube: '▶️' };

  function coverUrl(item) {
    if (item.audio_path) return '/api/cover/' + encodeURIComponent(item.id);
    if (item.cover) return '/api/proxy?url=' + encodeURIComponent(item.cover);
    return '';
  }

  function coverHtml(item, cls) {
    const url = coverUrl(item);
    if (!url) return `<div class="${cls}"><span>♪</span></div>`;
    return `<div class="${cls}"><img loading="lazy" src="${esc(url)}" alt=""
              onerror="this.style.display='none';this.parentNode.innerHTML='<span>♪</span>'" /></div>`;
  }

  // ======================================================================
  // 路由 / 视图
  // ======================================================================
  document.getElementById('nav').addEventListener('click', (e) => {
    const item = e.target.closest('.nav-item');
    if (!item) return;
    S.view = item.dataset.view;
    document.querySelectorAll('.nav-item').forEach((n) => n.classList.toggle('active', n === item));
    render();
  });

  document.getElementById('platformTabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.ptab');
    if (!btn) return;
    S.platform = btn.dataset.platform;
    document.querySelectorAll('.ptab').forEach((b) => b.classList.toggle('active', b === btn));
    if (S.keyword) doSearch();
  });

  $('#kw').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
  $('#btnSearch').addEventListener('click', doSearch);

  function render() {
    const c = $('#content');
    if (S.view === 'search') return renderSearch(c);
    if (S.view === 'favorites') return renderFavorites(c);
    if (S.view === 'history') return renderHistory(c);
    if (S.view === 'playlists') return renderPlaylists(c);
    if (S.view === 'tasks') return renderTasks(c);
    if (S.view === 'account') return renderAccount(c);
    if (S.view === 'settings') return renderSettings(c);
  }

  // ======================================================================
  // 搜索
  // ======================================================================
  async function doSearch() {
    const keyword = $('#kw').value.trim();
    if (!keyword) { toast('请输入关键词', 'warn'); return; }
    S.keyword = keyword;
    S.searching = true;
    S.view = 'search';
    document.querySelectorAll('.nav-item').forEach((n) =>
      n.classList.toggle('active', n.dataset.view === 'search'));
    renderSearch($('#content'), true);

    try {
      const data = await api(`/api/search?keyword=${encodeURIComponent(keyword)}&platform=${S.platform}&limit=20`);
      S.results = (data.items || []).map((it) => ({ ...it, _key: it.platform + ':' + it.source_id }));
      S.errors = data.errors || {};
      await refreshFavSet();
      S.searching = false;
      renderSearch($('#content'));
      const total = S.results.length;
      if (total === 0) {
        const reasons = Object.values(S.errors);
        toast(reasons.length ? reasons.join('；') : '没有搜到结果', 'warn', 5000);
      } else {
        toast(`找到 ${total} 条结果`, 'ok');
      }
    } catch (err) {
      S.searching = false;
      renderSearch($('#content'));
      toast('搜索失败：' + err.message, 'err', 5000);
    }
  }

  function renderSearch(c, loading = false) {
    if (loading) {
      c.innerHTML = `<div class="skeleton">${Array.from({ length: 6 }).map(() => `
        <div class="sk-row">
          <div class="sk-bar"></div>
          <div class="sk-bar sk-cover"></div>
          <div><div class="sk-bar" style="width:60%"></div>
               <div class="sk-bar" style="width:35%;margin-top:8px;height:9px"></div></div>
          <div class="sk-bar" style="width:70px;margin-left:auto"></div>
        </div>`).join('')}</div>`;
      return;
    }

    if (!S.results.length) {
      c.innerHTML = `<div class="empty">
        <div class="big">🔍</div>
        <h3>搜点什么吧</h3>
        <p>输入关键词，一键搜索小红书 / 抖音 / YouTube 的视频，<br/>
           点「转音频」把它变成可以在这里循环播放的音频。</p>
      </div>`;
      return;
    }

    const head = `<div class="sec-head">
      <h2>${esc(S.keyword)}</h2>
      <span class="sub">共 ${S.results.length} 条 · ${S.platform === 'all' ? '全部平台' : PLATFORM_LABEL[S.platform]}</span>
      <span class="spacer"></span>
      <button class="btn-ghost" id="btnExtractAll">全部转音频</button>
    </div>`;

    c.innerHTML = head + `<div class="track-list">${S.results.map(trackRow).join('')}</div>`;
    $('#btnExtractAll')?.addEventListener('click', extractAll);
  }

  function trackRow(item, index) {
    const key = item._key || item.platform + ':' + item.source_id;
    const state = jobStateOf(item);
    const actions = renderActions(item, state);
    const isPlaying = S.current && S.current.source_id === item.source_id
      && S.current.platform === item.platform;

    return `<div class="track ${isPlaying ? 'playing' : ''}" data-key="${esc(key)}">
      <div class="t-index">${index != null ? index + 1 : (isPlaying ? '♪' : '·')}</div>
      <div class="t-cover" data-act="play">
        ${coverHtml(item, 't-cover-inner')}
        <div class="t-play">${isPlaying && !audio.paused ? '❚❚' : '▶'}</div>
      </div>
      <div class="t-main">
        <div class="t-title">${esc(item.title || '(无标题)')}</div>
        <div class="t-sub">
          <span class="pill ${item.platform}">${PLATFORM_LABEL[item.platform] || item.platform}</span>
          ${state.status === 'running' ? `<span>转码中 ${Math.round(state.progress)}%</span>` : ''}
          ${state.status === 'failed' ? `<span style="color:#ff8a80">${esc(state.error || '失败')}</span>` : ''}
        </div>
      </div>
      <div class="t-author">${esc(item.author || '')}</div>
      <div class="t-dur">${item.duration ? fmtTime(item.duration) : '--:--'}</div>
      <div class="t-actions">${actions}</div>
    </div>`;
  }

  function jobStateOf(item) {
    const key = item._key || item.platform + ':' + item.source_id;
    const trackId = S.trackIds ? S.trackIds[key] : null;
    if (!trackId) return { status: item.status || 'idle', progress: 0, error: '', trackId: null };
    const job = S.jobs[trackId];
    if (job) return { ...job, trackId };
    return { status: item.status || 'idle', progress: 0, error: item.error || '', trackId };
  }

  function renderActions(item, state) {
    const fav = S.favs.has(state.trackId) || S.favs.has(item.id);
    const favBtn = `<button class="icon-btn ${fav ? 'on' : ''}" data-act="fav"
                      title="收藏">${fav ? '♥' : '♡'}</button>`;

    if (state.status === 'ready') {
      return `<button class="btn-ghost done" data-act="play">▶ 播放</button>
              <button class="btn-ghost" data-act="download">下载</button>${favBtn}`;
    }
    if (state.status === 'running' || state.status === 'queued') {
      return `<button class="btn-ghost busy" data-act="none">${Math.round(state.progress)}%</button>${favBtn}`;
    }
    if (state.status === 'failed') {
      return `<button class="btn-ghost fail" data-act="extract" title="${esc(state.error)}">重试</button>${favBtn}`;
    }
    return `<button class="btn-ghost" data-act="extract">转音频</button>${favBtn}`;
  }

  // 事件委托：搜索结果区
  $('#content').addEventListener('click', async (e) => {
    const row = e.target.closest('.track');
    if (!row) return;
    const actEl = e.target.closest('[data-act]');
    const act = actEl ? actEl.dataset.act : 'play';

    const item = findItem(row.dataset.key);
    if (!item) return;

    if (act === 'extract') return extractOne(item);
    if (act === 'play') return playItem(item);
    if (act === 'download') {
      const st = jobStateOf(item);
      if (st.trackId) window.open('/api/download/' + st.trackId, '_blank');
      return;
    }
    if (act === 'fav') {
      const st = jobStateOf(item);
      if (!st.trackId) { toast('请先转成音频再收藏', 'warn'); return; }
      return toggleFavorite(st.trackId);
    }
    if (act === 'remove') {
      const st = jobStateOf(item);
      if (st.trackId) return removeTrack(st.trackId);
    }
  });

  function findItem(key) {
    return S.results.find((r) => (r._key || r.platform + ':' + r.source_id) === key)
      || S.queue.find((q) => (q._key || q.platform + ':' + q.source_id) === key)
      || (S.parsedItem && (S.parsedItem._key || '') === key ? S.parsedItem : null)
      || null;
  }

  // ======================================================================
  // 转音频
  // ======================================================================
  async function extractOne(item, silent = false) {
    const key = item._key || item.platform + ':' + item.source_id;
    S.trackIds = S.trackIds || {};
    if (S.trackIds[key]) {
      const st = S.jobs[S.trackIds[key]];
      if (st && (st.status === 'running' || st.status === 'queued')) return S.trackIds[key];
    }
    try {
      const res = await api('/api/extract', {
        method: 'POST',
        body: JSON.stringify({
          platform: item.platform,
          source_id: item.source_id,
          title: item.title || '',
          author: item.author || '',
          cover: item.cover || '',
          duration: item.duration || 0,
          source_url: item.source_url || '',
          extra: item.extra || {},
        }),
      });
      S.trackIds[key] = res.track_id;
      S.jobs[res.track_id] = { status: res.status || 'queued', progress: 0, error: '' };
      if (!silent) toast(res.queued === false ? '已就绪' : '已加入转码队列', 'ok');
      render();
      startPolling();
      return res.track_id;
    } catch (err) {
      toast('提交失败：' + err.message, 'err');
      return null;
    }
  }

  async function extractAll() {
    let n = 0;
    for (const item of S.results) {
      const st = jobStateOf(item);
      if (st.status === 'idle' || st.status === 'failed') { await extractOne(item, true); n++; }
    }
    toast(n ? `已提交 ${n} 个转码任务` : '没有需要转换的项', n ? 'ok' : 'warn');
  }

  async function playItem(item) {
    const st = jobStateOf(item);
    if (st.status === 'ready' && st.trackId) {
      const track = await api('/api/track/' + st.trackId);
      enqueueAndPlay(track);
      return;
    }
    if (st.status === 'running' || st.status === 'queued') {
      toast(`正在转码 ${Math.round(st.progress)}%，完成后自动播放`, 'info');
      S.autoPlayAfter = st.trackId;
      return;
    }
    const trackId = await extractOne(item);
    if (trackId) {
      S.autoPlayAfter = trackId;
      toast('转码完成后会自动播放', 'info');
    }
  }

  // ======================================================================
  // 轮询任务状态
  // ======================================================================
  let pollTimer = null;
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(pollJobs, 1500);
  }

  async function pollJobs() {
    try {
      const data = await api('/api/jobs');
      let changed = false;
      const active = new Set();

      (data.active || []).forEach((t) => {
        active.add(t.id);
        const prev = S.jobs[t.id] || {};
        S.jobs[t.id] = { status: t.status, progress: t.progress || 0, error: t.error || '' };
        if (prev.status !== t.status || Math.abs((prev.progress || 0) - (t.progress || 0)) > 0.5) changed = true;
      });
      (data.failed || []).forEach((t) => {
        const prev = S.jobs[t.id] || {};
        S.jobs[t.id] = { status: 'failed', progress: 0, error: t.error || '' };
        if (prev.status !== 'failed') { changed = true; toast('转码失败：' + (t.title || '') + ' ' + (t.error || ''), 'err', 5000); }
      });

      // 已完成的：从队列里找，标记 ready
      Object.keys(S.trackIds || {}).forEach((key) => {
        const id = S.trackIds[key];
        if (active.has(id)) return;
        const job = S.jobs[id];
        if (job && (job.status === 'queued' || job.status === 'running')) {
          api('/api/track/' + id).then((t) => {
            S.jobs[id] = { status: t.status, progress: t.progress || 100, error: t.error || '' };
            if (t.status === 'ready') {
              markReady(key, t);
            } else if (t.status === 'failed') {
              toast('转码失败：' + (t.error || ''), 'err', 5000);
            }
            render();
          }).catch(() => {});
        }
      });

      const badge = $('#taskBadge');
      if (data.count > 0) { badge.textContent = data.count; badge.classList.remove('hidden'); }
      else badge.classList.add('hidden');

      if (changed) render();
      if (data.count === 0 && !Object.values(S.jobs).some((j) => j.status === 'queued' || j.status === 'running')) {
        clearInterval(pollTimer); pollTimer = null;
      }
    } catch (_) { /* 网络抖动忽略 */ }
  }

  function markReady(key, track) {
    S.jobs[track.id] = { status: 'ready', progress: 100, error: '' };
    const item = findItem(key);
    if (item) { item.status = 'ready'; item.audio_path = track.audio_path; item.id = track.id; }
    const q = S.queue.find((x) => x.id === track.id);
    if (q) { q.status = 'ready'; q.audio_path = track.audio_path; }
    if (S.autoPlayAfter === track.id) {
      S.autoPlayAfter = null;
      enqueueAndPlay(track);
    }
  }

  // ======================================================================
  // 播放器
  // ======================================================================
  function enqueueAndPlay(track) {
    let idx = S.queue.findIndex((t) => t.id === track.id);
    if (idx === -1) { S.queue.push(track); idx = S.queue.length - 1; }
    S.cursor = idx;
    playAt(idx);
  }

  function playAt(idx) {
    if (idx < 0 || idx >= S.queue.length) return;
    S.cursor = idx;
    const track = S.queue[idx];
    S.current = track;

    audio.src = '/api/stream/' + track.id;
    audio.volume = S.volume;
    audio.muted = S.muted;
    audio.play().then(() => {
      api('/api/history/' + track.id, { method: 'POST' }).catch(() => {});
    }).catch((err) => {
      toast('播放失败：' + err.message, 'err');
    });
    updatePlayerUI();
    render();
  }

  function togglePlay() {
    if (!S.current) {
      if (S.queue.length) playAt(0);
      else toast('播放队列是空的，先去搜点东西吧', 'warn');
      return;
    }
    if (audio.paused) audio.play().catch((e) => toast('播放失败：' + e.message, 'err'));
    else audio.pause();
  }

  function nextTrack(auto = false) {
    if (!S.queue.length) return;
    if (S.mode === 'shuffle') {
      S.cursor = Math.floor(Math.random() * S.queue.length);
      return playAt(S.cursor);
    }
    if (S.cursor + 1 < S.queue.length) return playAt(S.cursor + 1);
    if (auto && S.mode === 'single') return playAt(S.cursor);
    playAt(0);
  }

  function prevTrack() {
    if (!S.queue.length) return;
    if (audio.currentTime > 3) { audio.currentTime = 0; return; }
    playAt(S.cursor - 1 < 0 ? S.queue.length - 1 : S.cursor - 1);
  }

  function updatePlayerUI() {
    const t = S.current;
    $('#pbTitle').textContent = t ? (t.title || '(无标题)') : '未在播放';
    $('#pbAuthor').textContent = t
      ? `${t.author || ''}${t.platform ? ' · ' + (PLATFORM_LABEL[t.platform] || t.platform) : ''}`
      : '搜索并点击「转音频」开始';
    $('#pbCover').src = t ? (coverUrl(t) || '') : '';
    $('#btnPlay').textContent = audio.paused ? '▶' : '❚❚';
    $('#btnFav').classList.toggle('on', !!(t && S.favs.has(t.id)));
    $('#btnFav').textContent = (t && S.favs.has(t.id)) ? '♥' : '♡';
    $('#btnMode').textContent = S.mode === 'single' ? '🔂' : (S.mode === 'shuffle' ? '🔀' : '🔁');
    renderQueue();
  }

  audio.addEventListener('timeupdate', () => {
    const dur = audio.duration || 0;
    const pct = dur ? (audio.currentTime / dur) * 100 : 0;
    $('#fillProgress').style.width = pct + '%';
    $('#knobProgress').style.left = pct + '%';
    $('#curTime').textContent = fmtTime(audio.currentTime);
  });
  audio.addEventListener('loadedmetadata', () => { $('#totTime').textContent = fmtTime(audio.duration); });
  audio.addEventListener('play', updatePlayerUI);
  audio.addEventListener('pause', updatePlayerUI);
  audio.addEventListener('ended', () => nextTrack(true));
  audio.addEventListener('error', () => {
    if (S.current) toast('音频加载失败，文件可能已被清理', 'err');
  });

  $('#btnPlay').addEventListener('click', togglePlay);
  $('#btnNext').addEventListener('click', () => nextTrack(false));
  $('#btnPrev').addEventListener('click', prevTrack);
  $('#btnMode').addEventListener('click', () => {
    S.mode = S.mode === 'list' ? 'single' : (S.mode === 'single' ? 'shuffle' : 'list');
    localStorage.setItem('mh.mode', S.mode);
    toast({ list: '列表循环', single: '单曲循环', shuffle: '随机播放' }[S.mode], 'info', 1200);
    updatePlayerUI();
  });
  $('#btnFav').addEventListener('click', () => { if (S.current) toggleFavorite(S.current.id); });

  // 进度条拖动
  bindBar($('#barProgress'), (ratio) => {
    if (!audio.duration) return;
    audio.currentTime = ratio * audio.duration;
  });
  bindBar($('#barVolume'), (ratio) => {
    S.volume = Math.max(0, Math.min(1, ratio));
    audio.volume = S.volume;
    S.muted = false;
    audio.muted = false;
    localStorage.setItem('mh.volume', String(S.volume));
    updateVolumeUI();
  });
  $('#volIco').addEventListener('click', () => {
    S.muted = !S.muted; audio.muted = S.muted; updateVolumeUI();
  });

  function bindBar(el, onRatio) {
    if (!el) return;
    const handle = (e) => {
      const rect = el.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      onRatio(Math.max(0, Math.min(1, x / rect.width)));
    };
    let dragging = false;
    el.addEventListener('mousedown', (e) => { dragging = true; handle(e); });
    window.addEventListener('mousemove', (e) => { if (dragging) handle(e); });
    window.addEventListener('mouseup', () => { dragging = false; });
    el.addEventListener('click', handle);
  }

  function updateVolumeUI() {
    const pct = S.muted ? 0 : S.volume * 100;
    $('#fillVolume').style.width = pct + '%';
    $('#knobVolume').style.left = pct + '%';
    $('#volIco').textContent = S.muted || S.volume === 0 ? '🔇' : (S.volume < 0.5 ? '🔉' : '🔊');
  }

  // 键盘快捷键
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    if (e.code === 'ArrowRight' && e.shiftKey) nextTrack(false);
    if (e.code === 'ArrowLeft' && e.shiftKey) prevTrack();
  });

  // ======================================================================
  // 播放队列
  // ======================================================================
  $('#btnQueue').addEventListener('click', () => {
    $('.app').classList.toggle('queue-open');
  });
  $('#btnClearQueue').addEventListener('click', () => {
    S.queue = []; S.cursor = -1; S.current = null;
    audio.pause(); audio.removeAttribute('src');
    updatePlayerUI(); render(); toast('已清空播放队列', 'info');
  });

  function renderQueue() {
    const el = $('#queueList');
    if (!S.queue.length) {
      el.innerHTML = `<div class="empty" style="padding:32px 12px">
        <div class="big" style="font-size:30px">🎧</div><p>队列还是空的</p></div>`;
      return;
    }
    el.innerHTML = S.queue.map((t, i) => `
      <div class="q-item ${i === S.cursor ? 'playing' : ''}" data-qindex="${i}">
        ${coverHtml(t, 'q-cover')}
        <div class="q-main">
          <div class="q-title">${esc(t.title || '(无标题)')}</div>
          <div class="q-sub">${esc(t.author || '')}</div>
        </div>
        <button class="icon-btn" data-qremove="${i}" title="移除">✕</button>
      </div>`).join('');
  }

  $('#queueList').addEventListener('click', (e) => {
    const rm = e.target.closest('[data-qremove]');
    if (rm) {
      const i = Number(rm.dataset.qremove);
      S.queue.splice(i, 1);
      if (i < S.cursor) S.cursor--;
      else if (i === S.cursor) { S.current = null; audio.pause(); }
      updatePlayerUI(); render();
      return;
    }
    const row = e.target.closest('[data-qindex]');
    if (row) playAt(Number(row.dataset.qindex));
  });

  // ======================================================================
  // 收藏 / 历史 / 歌单
  // ======================================================================
  async function refreshFavSet() {
    try {
      const data = await api('/api/favorites');
      S.favs = new Set((data.items || []).map((t) => t.id));
    } catch (_) { /* ignore */ }
  }

  async function toggleFavorite(trackId) {
    const has = S.favs.has(trackId);
    try {
      await api('/api/favorites/' + trackId, { method: has ? 'DELETE' : 'POST' });
      if (has) S.favs.delete(trackId); else S.favs.add(trackId);
      toast(has ? '已取消收藏' : '已加入收藏', 'ok', 1400);
      updatePlayerUI();
      render();
    } catch (err) { toast('操作失败：' + err.message, 'err'); }
  }

  async function removeTrack(trackId) {
    if (!confirm('确定要删除这条音频吗？本地文件也会一并删除。')) return;
    try {
      await api('/api/track/' + trackId + '?remove_file=true', { method: 'DELETE' });
      S.favs.delete(trackId);
      S.queue = S.queue.filter((t) => t.id !== trackId);
      if (S.current && S.current.id === trackId) { audio.pause(); S.current = null; updatePlayerUI(); }
      toast('已删除', 'ok');
      render();
    } catch (err) { toast('删除失败：' + err.message, 'err'); }
  }

  function libraryRow(track, index, extraAction = '') {
    const playing = S.current && S.current.id === track.id;
    return `<div class="track ${playing ? 'playing' : ''}" data-lib="${esc(track.id)}">
      <div class="t-index">${index + 1}</div>
      <div class="t-cover" data-act="play">${coverHtml(track, 't-cover-inner')}
        <div class="t-play">${playing && !audio.paused ? '❚❚' : '▶'}</div></div>
      <div class="t-main">
        <div class="t-title">${esc(track.title || '(无标题)')}</div>
        <div class="t-sub"><span class="pill ${track.platform}">${PLATFORM_LABEL[track.platform] || track.platform}</span></div>
      </div>
      <div class="t-author">${esc(track.author || '')}</div>
      <div class="t-dur">${track.duration ? fmtTime(track.duration) : '--:--'}</div>
      <div class="t-actions">
        <button class="btn-ghost" data-act="play">▶ 播放</button>
        ${extraAction}
        <button class="icon-btn" data-act="fav" title="收藏">${S.favs.has(track.id) ? '♥' : '♡'}</button>
        <button class="icon-btn" data-act="remove" title="删除">🗑</button>
      </div>
    </div>`;
  }

  $('#content').addEventListener('click', async (e) => {
    const row = e.target.closest('[data-lib]');
    if (!row) return;
    const trackId = row.dataset.lib;
    const act = (e.target.closest('[data-act]') || {}).dataset?.act || 'play';
    const fromPlaylist = S.openPlaylist;

    if (act === 'play') {
      const tracks = await currentLibraryTracks();
      const idx = tracks.findIndex((t) => t.id === trackId);
      S.queue = tracks.map((t) => ({ ...t }));
      playAt(idx < 0 ? 0 : idx);
      return;
    }
    if (act === 'fav') return toggleFavorite(trackId);
    if (act === 'remove') {
      if (fromPlaylist) {
        await api(`/api/playlists/${fromPlaylist}/items/${trackId}`, { method: 'DELETE' });
        toast('已从歌单移除', 'ok'); render(); return;
      }
      return removeTrack(trackId);
    }
    if (act === 'add') {
      return addToPlaylistDialog(trackId);
    }
  });

  async function currentLibraryTracks() {
    if (S.view === 'favorites') return (await api('/api/favorites')).items || [];
    if (S.view === 'history') return (await api('/api/history')).items || [];
    if (S.view === 'playlists' && S.openPlaylist) return (await api('/api/playlists/' + S.openPlaylist)).items || [];
    if (S.view === 'tasks') return (await api('/api/tracks?status=ready&limit=50')).items || [];
    return [];
  }

  async function renderFavorites(c) {
    const data = await api('/api/favorites');
    const items = data.items || [];
    await refreshFavSet();
    if (!items.length) {
      c.innerHTML = `<div class="empty"><div class="big">❤️</div><h3>还没有收藏</h3>
        <p>在搜索结果或播放条上点 ♡ 就能收藏到这里。</p></div>`;
      return;
    }
    c.innerHTML = `<div class="sec-head"><h2>我的收藏</h2><span class="sub">${items.length} 首</span>
      <span class="spacer"></span><button class="btn-ghost" id="btnPlayFavs">▶ 播放全部</button></div>
      <div class="track-list">${items.map((t, i) => libraryRow(t, i,
        `<button class="btn-ghost" data-act="add">加入歌单</button>`)).join('')}</div>`;
    $('#btnPlayFavs')?.addEventListener('click', () => {
      S.queue = items.map((t) => ({ ...t })); playAt(0);
    });
  }

  async function renderHistory(c) {
    const data = await api('/api/history');
    const items = data.items || [];
    if (!items.length) {
      c.innerHTML = `<div class="empty"><div class="big">🕘</div><h3>还没有播放记录</h3></div>`;
      return;
    }
    c.innerHTML = `<div class="sec-head"><h2>最近播放</h2><span class="sub">${items.length} 首</span>
      <span class="spacer"></span><button class="btn-ghost" id="btnPlayHist">▶ 播放全部</button></div>
      <div class="track-list">${items.map((t, i) => libraryRow(t, i,
        `<button class="btn-ghost" data-act="add">加入歌单</button>`)).join('')}</div>`;
    $('#btnPlayHist')?.addEventListener('click', () => {
      S.queue = items.map((t) => ({ ...t })); playAt(0);
    });
  }

  async function renderPlaylists(c) {
    if (S.openPlaylist) {
      const [detail, lists] = await Promise.all([
        api('/api/playlists/' + S.openPlaylist),
        api('/api/playlists'),
      ]);
      const meta = (lists.items || []).find((p) => p.id === S.openPlaylist) || {};
      const items = detail.items || [];
      c.innerHTML = `<div class="sec-head">
        <button class="btn-ghost" id="btnBackPl">← 返回</button>
        <h2>${esc(meta.name || '歌单')}</h2><span class="sub">${items.length} 首</span>
        <span class="spacer"></span>
        <button class="btn-ghost" id="btnPlayPl">▶ 播放全部</button></div>
        ${items.length ? `<div class="track-list">${items.map((t, i) => libraryRow(t, i)).join('')}</div>`
          : `<div class="empty"><div class="big">📁</div><h3>这个歌单还是空的</h3>
             <p>在收藏或最近播放里点「加入歌单」。</p></div>`}`;
      $('#btnBackPl').addEventListener('click', () => { S.openPlaylist = null; render(); });
      $('#btnPlayPl')?.addEventListener('click', () => {
        S.queue = items.map((t) => ({ ...t })); playAt(0);
      });
      return;
    }

    const data = await api('/api/playlists');
    const lists = data.items || [];
    c.innerHTML = `<div class="sec-head"><h2>我的歌单</h2><span class="sub">${lists.length} 个</span>
      <span class="spacer"></span><button class="btn-ghost" id="btnNewPl">＋ 新建歌单</button></div>
      <div class="card-grid">${lists.map((p) => `
        <div class="card" data-pl="${p.id}">
          <div class="card-cover">📁</div>
          <div class="card-title">${esc(p.name)}</div>
          <div class="card-sub">${p.count} 首</div>
          <div class="card-actions"><button class="btn-ghost" data-pl-open="${p.id}">打开</button>
            <button class="btn-ghost" data-pl-del="${p.id}">删除</button></div>
        </div>`).join('')}</div>`;

    $('#btnNewPl').addEventListener('click', async () => {
      const name = prompt('歌单名称：');
      if (!name) return;
      await api('/api/playlists', { method: 'POST', body: JSON.stringify({ name }) });
      toast('已创建', 'ok'); render();
    });
    c.querySelectorAll('[data-pl-open]').forEach((b) => b.addEventListener('click', () => {
      S.openPlaylist = Number(b.dataset.plOpen); render();
    }));
    c.querySelectorAll('[data-pl-del]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('删除这个歌单？（里面的音频不会被删除）')) return;
      await api('/api/playlists/' + b.dataset.plDel, { method: 'DELETE' });
      toast('已删除', 'ok'); render();
    }));
  }

  async function addToPlaylistDialog(trackId) {
    const data = await api('/api/playlists');
    const lists = data.items || [];
    if (!lists.length) { toast('还没有歌单，请先新建', 'warn'); return; }
    const options = lists.map((p, i) => `${i + 1}. ${p.name}`).join('\n');
    const pick = prompt(`加入哪个歌单？\n${options}`, '1');
    if (!pick) return;
    const idx = Number(pick) - 1;
    if (!lists[idx]) { toast('无效的选择', 'warn'); return; }
    await api(`/api/playlists/${lists[idx].id}/items?track_id=${encodeURIComponent(trackId)}`,
      { method: 'POST' });
    toast(`已加入「${lists[idx].name}」`, 'ok');
  }

  // ======================================================================
  // 任务队列视图
  // ======================================================================
  async function renderTasks(c) {
    const data = await api('/api/jobs');
    const active = data.active || [];
    const failed = data.failed || [];
    const done = (await api('/api/tracks?status=ready&limit=50')).items || [];

    c.innerHTML = `<div class="sec-head"><h2>任务队列</h2>
      <span class="sub">进行中 ${active.length} · 失败 ${failed.length}</span></div>
      ${active.length ? `<div class="track-list">${active.map((t, i) => `
        <div class="track">
          <div class="t-index">${i + 1}</div>
          <div class="t-cover">${coverHtml(t, 't-cover-inner')}</div>
          <div class="t-main"><div class="t-title">${esc(t.title || t.source_id)}</div>
            <div class="t-sub"><span class="pill ${t.platform}">${PLATFORM_LABEL[t.platform] || t.platform}</span>
            <span>${t.status === 'queued' ? '排队中' : '转码中'} ${Math.round(t.progress || 0)}%</span></div></div>
          <div class="t-author">${esc(t.author || '')}</div>
          <div class="t-dur">--:--</div><div class="t-actions"></div>
        </div>`).join('')}</div>` : `<div class="empty"><div class="big">✅</div><h3>没有进行中的任务</h3></div>`}
      ${failed.length ? `<div class="sec-head" style="margin-top:28px"><h2>失败</h2></div>
        <div class="track-list">${failed.map((t, i) => `
          <div class="track">
            <div class="t-index">${i + 1}</div>
            <div class="t-cover">${coverHtml(t, 't-cover-inner')}</div>
            <div class="t-main"><div class="t-title">${esc(t.title || t.source_id)}</div>
              <div class="t-sub" style="color:#ff8a80">${esc(t.error || '未知错误')}</div></div>
            <div class="t-author"></div><div class="t-dur">--:--</div>
            <div class="t-actions"><button class="btn-ghost" data-retry="${t.id}">重试</button>
              <button class="icon-btn" data-drop="${t.id}">🗑</button></div>
          </div>`).join('')}</div>` : ''}
      <div class="sec-head" style="margin-top:28px"><h2>已完成</h2>
        <span class="sub">${done.length} 首（最近 50）</span></div>
      <div class="track-list">${done.map((t, i) => libraryRow(t, i,
        `<button class="btn-ghost" data-act="add">加入歌单</button>`)).join('')
        || `<div class="empty"><p>还没有转好的音频</p></div>`}</div>`;

    c.querySelectorAll('[data-retry]').forEach((b) => b.addEventListener('click', async () => {
      await api('/api/extract', {
        method: 'POST',
        body: JSON.stringify(await api('/api/track/' + b.dataset.retry).then((t) => ({
          platform: t.platform, source_id: t.source_id, title: t.title, author: t.author,
          cover: t.cover, duration: t.duration, source_url: t.source_url, extra: t.extra,
        }))),
      });
      toast('已重新入队', 'ok'); startPolling(); render();
    }));
    c.querySelectorAll('[data-drop]').forEach((b) => b.addEventListener('click', () =>
      removeTrack(b.dataset.drop)));
  }

  // ======================================================================
  // 设置
  // ======================================================================
  async function renderSettings(c) {
    const [platforms, stats, health] = await Promise.all([
      api('/api/platforms'), api('/api/stats'), api('/api/health'),
    ]);
    const rows = (platforms.items || []).map((p) => `
      <div class="status-line">
        <span class="dot ${p.available ? 'ok' : 'bad'}"></span>
        <b style="width:80px;display:inline-block">${esc(p.label)}</b>
        <span style="color:var(--text-dim)">${p.available ? '可用' : esc(p.reason)}</span>
      </div>`).join('');

    c.innerHTML = `
      <div class="sec-head"><h2>设置</h2></div>

      <div class="form-card">
        <h3>平台状态</h3>
        <p class="hint">小红书与抖音的接口都要求登录态，必须在 <code>.env</code> 里配置 Cookie 后重启容器。</p>
        ${rows}
      </div>

      <div class="form-card">
        <h3>粘贴链接解析</h3>
        <p class="hint">支持小红书 / 抖音 / YouTube 的分享链接。抖音与小红书同样需要 Cookie。</p>
        <div class="field">
          <input id="parseUrl" type="text" placeholder="https://www.xiaohongshu.com/explore/... 或 https://v.douyin.com/..." />
        </div>
        <button class="btn-primary" id="btnParse">解析</button>
        <div id="parseResult" style="margin-top:14px"></div>
      </div>

      <div class="form-card">
        <h3>运行信息</h3>
        <div class="status-line"><span class="dot ok"></span>曲目总数：<b>${stats.tracks || 0}</b>
          （就绪 ${(stats.by_status || {}).ready || 0} / 失败 ${(stats.by_status || {}).failed || 0}）</div>
        <div class="status-line"><span class="dot ok"></span>收藏：<b>${stats.favorites || 0}</b> · 播放记录：<b>${stats.history || 0}</b></div>
        <div class="status-line"><span class="dot ok"></span>音频缓存占用：<b>${esc(stats.cache_bytes_human || '0 B')}</b></div>
        <div class="status-line"><span class="dot ${health.tools && health.tools.ffmpeg && !health.tools.ffmpeg.startsWith('unavailable') ? 'ok' : 'bad'}"></span>
          ${esc((health.tools || {}).ffmpeg || 'ffmpeg 未检测到')}</div>
        <div class="status-line"><span class="dot ${health.tools && health.tools['yt-dlp'] && !health.tools['yt-dlp'].startsWith('unavailable') ? 'ok' : 'bad'}"></span>
          yt-dlp ${esc((health.tools || {})['yt-dlp'] || '未检测到')}</div>
        <div style="margin-top:14px">
          <button class="btn-ghost" id="btnClearCache">清理搜索缓存</button>
        </div>
      </div>`;

    $('#btnParse').addEventListener('click', async () => {
      const url = $('#parseUrl').value.trim();
      if (!url) return;
      const box = $('#parseResult');
      box.innerHTML = '<div class="status-line"><span class="dot warn"></span>解析中…</div>';
      try {
        const data = await api('/api/parse', { method: 'POST', body: JSON.stringify({ url }) });
        const it = data.item;
        it._key = it.platform + ':' + it.source_id;
        S.parsedItem = it;
        S.results = [it];
        S.keyword = it.title || '解析结果';
        box.innerHTML = '<div class="status-line"><span class="dot ok"></span>解析成功，已跳到搜索页</div>';
        S.view = 'search';
        document.querySelectorAll('.nav-item').forEach((n) =>
          n.classList.toggle('active', n.dataset.view === 'search'));
        renderSearch($('#content'));
      } catch (err) {
        box.innerHTML = `<div class="status-line"><span class="dot bad"></span>${esc(err.message)}</div>`;
      }
    });

    $('#btnClearCache').addEventListener('click', async () => {
      const r = await api('/api/cache/clear', { method: 'POST' });
      toast(`已清理 ${r.removed} 条缓存`, 'ok');
    });
  }

  // ======================================================================
  // 账号登录（扫码）
  //
  // 小红书 / 抖音的登录接口都只认真实浏览器，所以二维码由后端的
  // login-agent 容器（Chromium）生成并截图，这里只负责展示 + 轮询。
  // ======================================================================
  const PLATFORM_META = {
    xhs:     { label: '小红书', logo: '书', cls: 'xhs',     hint: '打开「小红书」App → 左上角菜单 → 扫一扫' },
    douyin:  { label: '抖音',   logo: '抖', cls: 'douyin',  hint: '打开「抖音」App → 左上角 → 扫一扫' },
    youtube: { label: 'YouTube', logo: '▶', cls: 'youtube', hint: '' },
  };

  let loginState = { platform: null, sid: null, timer: null };

  async function renderAccount(c) {
    c.innerHTML = '<div class="account-grid"><div class="account-card"><div class="ac-sub">加载中…</div></div></div>';
    let data;
    try {
      data = await api('/api/login/status');
    } catch (err) {
      c.innerHTML = `<div class="account-card"><div class="ac-sub">获取登录状态失败：${esc(err.message)}</div></div>`;
      return;
    }
    updateLoginBadge(data);

    const cards = (data.platforms || []).map((p) => {
      const meta = PLATFORM_META[p.platform] || { label: p.platform, logo: '?', cls: '' };
      const on = !!p.logged_in;
      const sub = on
        ? `已登录${p.nickname ? ' · ' + esc(p.nickname) : ''}`
        : '未登录 —— 该平台无法搜索';
      let actions = '';
      if (p.platform === 'youtube') {
        actions = '<span class="ac-sub">YouTube 不需要登录，配置好代理即可直接搜索</span>';
      } else if (on) {
        actions = `<button class="btn-ghost" data-logout="${p.platform}">退出登录</button>
                   <button class="btn-ghost" data-login="${p.platform}">重新扫码</button>`;
      } else {
        actions = `<button class="btn-primary" data-login="${p.platform}">扫码登录</button>`;
      }
      return `
        <div class="account-card">
          <div class="ac-head">
            <div class="ac-logo ${meta.cls}">${meta.logo}</div>
            <div style="flex:1;min-width:0">
              <div class="ac-name">${esc(meta.label)}</div>
              <div class="ac-sub">${sub}</div>
            </div>
            <span class="ac-state ${on ? 'on' : 'off'}">${on ? '已登录' : '未登录'}</span>
          </div>
          <div class="ac-foot">${actions}</div>
        </div>`;
    }).join('');

    const notice = data.enabled
      ? (data.agent_ready ? '' : '<div class="account-card" style="margin-top:14px"><div class="ac-sub">登录服务还在启动中（首次需要加载浏览器内核，约 10~30 秒），稍后重试即可。</div></div>')
      : '<div class="account-card" style="margin-top:14px"><div class="ac-sub">扫码登录已被 LOGIN_ENABLED 关闭。</div></div>';

    c.innerHTML = `
      <div style="margin-bottom:16px">
        <h2 style="margin:0 0 6px;font-size:19px">账号登录</h2>
        <p style="margin:0;color:var(--text-dim);font-size:13px">
          小红书和抖音都需要登录才能搜索。点「扫码登录」，用手机 App 扫一扫即可，登录状态会保存在服务端。
        </p>
      </div>
      <div class="account-grid">${cards}</div>
      ${notice}
    `;
  }

  function openLoginModal(platform) {
    const meta = PLATFORM_META[platform] || {};
    $('#loginTitle').textContent = '扫码登录 · ' + (meta.label || platform);
    $('#loginModal').classList.remove('hidden');
    startLogin(platform);
  }

  function closeLoginModal() {
    $('#loginModal').classList.add('hidden');
    if (loginState.timer) { clearTimeout(loginState.timer); loginState.timer = null; }
    if (loginState.sid) {
      api(`/api/login/${loginState.platform}/cancel/${loginState.sid}`, { method: 'POST' }).catch(() => {});
    }
    loginState = { platform: null, sid: null, timer: null };
  }

  function setLoginStatus(text, cls) {
    const el = $('#loginStatus');
    el.textContent = text;
    el.className = 'login-status' + (cls ? ' ' + cls : '');
  }

  async function startLogin(platform) {
    if (loginState.timer) { clearTimeout(loginState.timer); loginState.timer = null; }
    if (loginState.sid) {
      api(`/api/login/${loginState.platform}/cancel/${loginState.sid}`, { method: 'POST' }).catch(() => {});
    }
    loginState.platform = platform;
    loginState.sid = null;
    const img = $('#loginQr');
    img.removeAttribute('src');
    $('#qrLoading').classList.remove('hidden');
    $('#loginHint').textContent = '';
    setLoginStatus('正在打开登录页…（首次较慢）', '');

    try {
      const data = await api(`/api/login/${platform}/start`, { method: 'POST' });
      loginState.sid = data.session_id;
      img.src = data.qrcode;
      $('#qrLoading').classList.add('hidden');
      setLoginStatus('请用手机 App 扫码', '');
      const meta = PLATFORM_META[platform] || {};
      $('#loginHint').textContent = meta.hint || '';
      pollLogin();
    } catch (err) {
      $('#qrLoading').classList.add('hidden');
      setLoginStatus('获取二维码失败：' + err.message, 'err');
    }
  }

  async function pollLogin() {
    if (!loginState.sid) return;
    try {
      const data = await api(`/api/login/${loginState.platform}/poll/${loginState.sid}`);
      if (data.status === 'success') {
        setLoginStatus('登录成功！', 'ok');
        $('#loginHint').textContent = data.nickname ? ('账号：' + data.nickname) : '凭据已保存';
        toast('登录成功，可以开始搜索了', 'ok');
        setTimeout(() => {
          closeLoginModal();
          if (S.view === 'account') render();
          refreshLoginStatus();
        }, 1200);
        return;
      }
      if (data.status === 'expired') {
        setLoginStatus('二维码已过期，请点「刷新二维码」', 'err');
        return;
      }
      if (data.status === 'error') {
        setLoginStatus(data.message || '浏览器会话异常', 'err');
        return;
      }
      if (data.qrcode && !$('#loginQr').getAttribute('src')) $('#loginQr').src = data.qrcode;
      loginState.timer = setTimeout(pollLogin, 1500);
    } catch (err) {
      setLoginStatus('轮询失败：' + err.message, 'err');
      loginState.timer = setTimeout(pollLogin, 3000);
    }
  }

  async function refreshLoginStatus() {
    try { updateLoginBadge(await api('/api/login/status')); } catch (_) { /* ignore */ }
  }

  function updateLoginBadge(data) {
    const badge = $('#loginBadge');
    if (!badge) return;
    const missing = (data.platforms || []).filter((p) => !p.logged_in && p.platform !== 'youtube');
    badge.classList.toggle('hidden', missing.length === 0);
  }

  async function doLogout(platform) {
    try {
      await api('/api/login/' + platform, { method: 'DELETE' });
      toast('已退出登录', 'ok');
      render();
      refreshLoginStatus();
    } catch (err) {
      toast('退出失败：' + err.message, 'err');
    }
  }

  function bindLoginUI() {
    $('#btnLoginClose').addEventListener('click', closeLoginModal);
    document.querySelectorAll('[data-login-close]').forEach((el) =>
      el.addEventListener('click', closeLoginModal));
    $('#btnLoginRefresh').addEventListener('click', () => {
      if (loginState.platform) startLogin(loginState.platform);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !$('#loginModal').classList.contains('hidden')) closeLoginModal();
    });
    // 账号卡片上的按钮走事件委托，避免每次 render 重新绑定
    $('#content').addEventListener('click', (e) => {
      const loginBtn = e.target.closest('[data-login]');
      if (loginBtn) { openLoginModal(loginBtn.dataset.login); return; }
      const logoutBtn = e.target.closest('[data-logout]');
      if (logoutBtn) { doLogout(logoutBtn.dataset.logout); }
    });
  }

  // ======================================================================
  // 侧边栏存储信息
  // ======================================================================
  async function refreshSidebar() {
    try {
      const s = await api('/api/stats');
      $('#storageInfo').innerHTML =
        `曲目 <b>${s.tracks || 0}</b> · 收藏 <b>${s.favorites || 0}</b><br/>缓存 <b>${esc(s.cache_bytes_human || '0 B')}</b>`;
    } catch (_) { /* ignore */ }
  }

  // ======================================================================
  // 启动
  // ======================================================================
  function boot() {
    S.trackIds = {};
    updateVolumeUI();
    updatePlayerUI();
    render();
    refreshFavSet().then(render);
    refreshSidebar();
    startPolling();
    bindLoginUI();
    refreshLoginStatus();
    setInterval(refreshSidebar, 30000);
    api('/api/health').catch(() => toast('后端未就绪，请稍候重试', 'err'));
  }

  boot();
})();
