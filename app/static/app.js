(() => {
  const els = {
    q: document.getElementById('q'),
    closeOverlay: document.getElementById('close-overlay'),
    searchStatus: document.getElementById('search-status'),
    grid: document.getElementById('grid'),
    results: document.getElementById('results'),
    player: document.getElementById('player'),
    video: document.getElementById('video'),
    playBtn: document.getElementById('play-btn'),
    airplayBtn: document.getElementById('airplay-btn'),
    footerChannel: document.getElementById('footer-channel'),
    footerMeta: document.getElementById('footer-meta'),
    footerStatus: document.getElementById('footer-status'),
    importModal: document.getElementById('import-modal'),
    categoryGrid: document.getElementById('category-grid'),
    importBtn: document.getElementById('import-btn'),
    importProgress: document.getElementById('import-progress'),
    progressFill: document.getElementById('progress-fill'),
    progressText: document.getElementById('progress-text'),
  };

  let currentHls = null;
  let currentId = null;
  let currentName = null;
  let currentCategory = null;
  let currentSource = null;
  let searchSeq = 0;
  let retryCount = 0;
  let stallTimer = null;
  let reloadTimer = null;
  let bufferTimer = null;
  let activeCategoryFilter = null;
  let epgCache = null;
  let epgFetchTimer = null;
  const RETRY_DELAYS_MS = [0, 2000, 5000, 10000, 20000, 30000, 45000, 60000];
  const MAX_RETRIES = RETRY_DELAYS_MS.length;
  const STALL_MS = 8000;

  const cleanName = (name) => (name || '').replace(/\s*search-ace\.stream\s*/gi, '').trim();

  const fmtTime = (ts) => {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  async function fetchEpg() {
    if (epgFetchTimer) clearTimeout(epgFetchTimer);
    try {
      const r = await fetch('/api/epg/now');
      if (r.ok) {
        const data = await r.json();
        epgCache = {};
        for (const item of data) {
          epgCache[item.content_id] = item;
        }
        applyEpgToCards();
      }
    } catch (e) {}
    epgFetchTimer = setTimeout(fetchEpg, 5 * 60 * 1000);
  }

  function applyEpgToCards() {
    if (!epgCache) return;
    document.querySelectorAll('.card').forEach((card) => {
      const id = card.dataset.id;
      const epg = epgCache[id];
      let el = card.querySelector('.epg-info');
      if (!el) {
        el = document.createElement('div');
        el.className = 'epg-info';
        card.appendChild(el);
      }
      if (epg && epg.title) {
        const next = epg.next_title ? ` · Next: ${epg.next_title}` : '';
        el.textContent = `${epg.title} (${fmtTime(epg.start)}–${fmtTime(epg.stop)})${next}`;
        el.hidden = false;
      } else {
        el.hidden = true;
      }
    });
  }

  const updateBuffer = () => {
    if (!currentId) return;
    const v = els.video;
    if (v.buffered.length === 0) return;
    const pos = v.currentTime;
    let end = 0;
    for (let i = 0; i < v.buffered.length; i++) {
      if (v.buffered.start(i) <= pos && v.buffered.end(i) > pos) {
        end = v.buffered.end(i);
        break;
      }
    }
    const ahead = Math.max(0, end - pos);
    if (ahead >= 5) setPlayerStatus('good');
    else if (ahead >= 2) setPlayerStatus('warn');
    else setPlayerStatus('bad');
  };

  const startBufferMonitor = () => {
    stopBufferMonitor();
    bufferTimer = setInterval(updateBuffer, 1000);
  };

  const stopBufferMonitor = () => {
    if (bufferTimer) { clearInterval(bufferTimer); bufferTimer = null; }
  };

  const debounce = (fn, ms) => {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  };

  const setSearchStatus = (msg, isErr = false) => {
    els.searchStatus.textContent = msg || '';
    els.searchStatus.classList.toggle('err', !!isErr);
  };

  const setPlayerStatus = (state) => {
    const dot = els.footerStatus;
    dot.className = 'status-dot';
    if (state === 'loading') dot.classList.add('spinner');
    else if (state === 'good') dot.classList.add('green');
    else if (state === 'warn') dot.classList.add('yellow');
    else if (state === 'bad') dot.classList.add('red');
  };

  const updateFooter = () => {
    if (currentName) {
      els.footerChannel.textContent = currentName;
      const chips = [];
      if (currentCategory) chips.push(currentCategory);
      if (currentSource) chips.push(currentSource);
      els.footerMeta.textContent = chips.join(' · ');
    } else {
      els.footerChannel.textContent = 'No channel selected';
      els.footerMeta.textContent = '';
    }
  };

  function updateResultsVisibility() {
    const hasQuery = !!els.q.value.trim();
    const isPlaying = !!currentId;
    const hidden = isPlaying && !hasQuery;
    els.results.hidden = hidden;
    els.closeOverlay.hidden = hidden || !isPlaying;
  }

  async function search() {
    const seq = ++searchSeq;
    const q = els.q.value.trim();
    updateResultsVisibility();

    if (!q && !activeCategoryFilter) {
      els.grid.innerHTML = '';
      setSearchStatus('');
      return;
    }
    setSearchStatus('Searching…');
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (activeCategoryFilter) params.set('category', activeCategoryFilter);
    try {
      const r = await fetch('/api/streams?' + params);
      const data = await r.json();
      if (seq !== searchSeq) return;
      render(data.results || []);
      setSearchStatus(
        data.results.length
          ? `${data.results.length} channels`
          : 'No matches',
      );
    } catch (e) {
      setSearchStatus('Search failed: ' + e.message, true);
    }
  }

  function render(rows) {
    els.grid.innerHTML = '';
    const frag = document.createDocumentFragment();
    for (const row of rows) {
      const card = document.createElement('div');
      card.className = 'card';
      card.tabIndex = 0;
      card.dataset.id = row.content_id;
      if (row.content_id === currentId) card.classList.add('playing');
      card.innerHTML = `<div class="name"></div>`;
      card.querySelector('.name').textContent = cleanName(row.name) || row.content_id;
      const open = () => play(row);
      card.addEventListener('click', open);
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      });
      frag.appendChild(card);
    }
    els.grid.appendChild(frag);
  }

  function clearStallTimer() {
    if (stallTimer) { clearTimeout(stallTimer); stallTimer = null; }
  }

  function clearReloadTimer() {
    if (reloadTimer) { clearTimeout(reloadTimer); reloadTimer = null; }
  }

  function stopCurrent() {
    clearStallTimer();
    clearReloadTimer();
    stopBufferMonitor();
    setPlayerStatus('');
    if (currentHls) { try { currentHls.destroy(); } catch (e) {} currentHls = null; }
    els.video.pause();
    if (currentId) {
      const id = currentId;
      navigator.sendBeacon
        ? navigator.sendBeacon('/api/stop/' + id)
        : fetch('/api/stop/' + id, { method: 'POST', keepalive: true }).catch(() => {});
    }
    currentId = null;
    currentName = null;
    currentCategory = null;
    currentSource = null;
    updateFooter();
  }

  function reloadStream(reason) {
    if (!currentId) return;
    if (reloadTimer) return;
    clearStallTimer();
    stopBufferMonitor();
    if (retryCount >= MAX_RETRIES) {
      setPlayerStatus('bad');
      return;
    }
    const delay = RETRY_DELAYS_MS[retryCount];
    retryCount++;
    const startedFor = currentId;
    const tick = () => {
      const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      if (remaining > 0) {
        setPlayerStatus('spinner');
      }
    };
    const deadline = Date.now() + delay;
    tick();
    const ticker = setInterval(tick, 1000);

    reloadTimer = setTimeout(() => {
      clearInterval(ticker);
      reloadTimer = null;
      if (currentId !== startedFor) return;
      setPlayerStatus('spinner');
      const url = `/hls/${startedFor}/index.m3u8?_=${Date.now()}`;
      if (currentHls) { try { currentHls.destroy(); } catch (e) {} currentHls = null; }
      if (els.video.canPlayType('application/vnd.apple.mpegurl')) {
        els.video.src = url;
        els.video.load();
        tryPlay();
      } else if (window.Hls && Hls.isSupported()) {
        const hls = new Hls({ liveSyncDurationCount: 3, manifestLoadingMaxRetry: 6 });
        hls.loadSource(url);
        hls.attachMedia(els.video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => tryPlay());
        hls.on(Hls.Events.ERROR, (_e, data) => {
          if (data.fatal) reloadStream('hls.js fatal');
        });
        currentHls = hls;
      }
    }, delay);
  }

  function tryPlay() {
    return els.video.play().catch(() => {
      els.playBtn.hidden = false;
    });
  }

  function hideOverlay() {
    els.q.value = '';
    els.results.hidden = true;
    updateResultsVisibility();
  }

  function play(row) {
    if (row.content_id === currentId) return;
    stopCurrent();

    currentId = row.content_id;
    currentName = cleanName(row.name) || row.content_id;
    currentCategory = row.category || null;
    currentSource = cleanName(row.source) || null;
    hideOverlay();
    updateFooter();
    retryCount = 0;
    setPlayerStatus('loading');
    els.playBtn.hidden = true;

    document.querySelectorAll('.card.playing').forEach((c) => c.classList.remove('playing'));
    const card = els.grid.querySelector(`.card[data-id="${row.content_id}"]`);
    if (card) card.classList.add('playing');

    const url = `/hls/${row.content_id}/index.m3u8`;
    if (els.video.canPlayType('application/vnd.apple.mpegurl')) {
      els.video.src = url;
      const onLoaded = () => {
        els.video.removeEventListener('loadedmetadata', onLoaded);
        tryPlay();
      };
      els.video.addEventListener('loadedmetadata', onLoaded);
      tryPlay();
    } else if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({ liveSyncDurationCount: 3, manifestLoadingMaxRetry: 6 });
      hls.loadSource(url);
      hls.attachMedia(els.video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => tryPlay());
        hls.on(Hls.Events.ERROR, (_e, data) => {
          if (data.fatal) reloadStream('hls.js fatal');
        });
      currentHls = hls;
    } else {
      setPlayerStatus('bad');
      return;
    }

    const onPlaying = () => {
      setPlayerStatus('good');
      startBufferMonitor();
      els.video.removeEventListener('playing', onPlaying);
    };
    els.video.addEventListener('playing', onPlaying);
  }

  els.video.addEventListener('playing', () => {
    retryCount = 0;
    clearStallTimer();
    clearReloadTimer();
    els.playBtn.hidden = true;
    startBufferMonitor();
  });
  els.video.addEventListener('pause', () => {
    if (currentId) els.playBtn.hidden = false;
  });
  els.video.addEventListener('webkitendfullscreen', () => {
    if (currentId) els.video.play().catch(() => {});
  });
  els.video.addEventListener('error', () => reloadStream('error'));
  els.video.addEventListener('ended', () => reloadStream('ended'));
  els.video.addEventListener('waiting', () => {
    clearStallTimer();
    stallTimer = setTimeout(() => {
      if (els.video.paused) return;
      if (els.video.readyState >= 3) return;
      reloadStream('stall');
    }, STALL_MS);
  });
  els.video.addEventListener('stalled', () => {
    if (!stallTimer) {
      stallTimer = setTimeout(() => {
        if (!els.video.paused && els.video.readyState < 3) reloadStream('stalled');
      }, STALL_MS);
    }
  });

  els.playBtn.addEventListener('click', () => {
    els.playBtn.hidden = true;
    tryPlay();
  });

  els.results.addEventListener('click', (e) => {
    if (e.target.closest('.card')) return;
    hideOverlay();
  });

  els.q.addEventListener('input', debounce(search, 250));
  els.q.addEventListener('focus', () => {
    els.results.hidden = false;
    if (currentId) els.closeOverlay.hidden = false;
  });
  els.closeOverlay.addEventListener('click', () => {
    els.q.blur();
    hideOverlay();
  });

  if (els.video.webkitShowPlaybackTargetPicker) {
    els.video.addEventListener('webkitplaybacktargetavailabilitychanged', (e) => {
      els.airplayBtn.hidden = e.availability !== 'available';
    });
    els.airplayBtn.addEventListener('click', () => {
      els.video.webkitShowPlaybackTargetPicker();
    });
  }

  window.addEventListener('pagehide', stopCurrent);

  const handleOrientation = () => {
    if (!currentId) return;
    const isLandscape = window.matchMedia('(orientation: landscape)').matches;
    if (isLandscape) {
      if (els.video.webkitEnterFullscreen) {
        els.video.webkitEnterFullscreen();
      } else if (els.video.requestFullscreen) {
        els.video.requestFullscreen().catch(() => {});
      }
    } else if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    }
  };
  window.addEventListener('orientationchange', handleOrientation);
  window.matchMedia('(orientation: landscape)').addEventListener('change', handleOrientation);

  // --- Import flow ---

  async function loadCategories() {
    const r = await fetch('/api/categories/predefined');
    const data = await r.json();
    const selected = new Set();

    els.categoryGrid.innerHTML = '';
    for (const cat of data.categories) {
      const chip = document.createElement('div');
      chip.className = 'category-chip';
      chip.textContent = cat.replace(/_/g, ' ');
      chip.dataset.category = cat;
      chip.addEventListener('click', () => {
        if (selected.has(cat)) {
          selected.delete(cat);
          chip.classList.remove('selected');
        } else {
          selected.add(cat);
          chip.classList.add('selected');
        }
        els.importBtn.disabled = selected.size === 0;
      });
      els.categoryGrid.appendChild(chip);
    }

    els.importBtn.addEventListener('click', async () => {
      els.importBtn.disabled = true;
      els.importProgress.hidden = false;
      const cats = [...selected];
      const total = cats.length;

      const r = await fetch('/api/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ categories: cats }),
      });
      const result = await r.json();

      for (let i = 0; i < total; i++) {
        const pct = Math.round(((i + 1) / total) * 100);
        els.progressFill.style.width = pct + '%';
        const catName = cats[i].replace(/_/g, ' ');
        const count = result.per_category[cats[i]] || 0;
        els.progressText.textContent = `Imported ${catName}: ${count} channels (${i + 1}/${total})`;
      }

      els.progressText.textContent = `Done! ${result.total} channels imported.`;
      setTimeout(() => {
        els.importModal.hidden = true;
        loadAllChannels();
      }, 1500);
    });
  }

  async function checkImportStatus() {
    const r = await fetch('/api/import-status');
    const data = await r.json();
    if (!data.imported) {
      els.importModal.hidden = false;
      await loadCategories();
    } else {
      loadAllChannels();
    }
  }

  async function loadAllChannels() {
    setSearchStatus('Loading channels…');
    try {
      const r = await fetch('/api/streams');
      const data = await r.json();
      render(data.results || []);
      fetchEpg();
      setSearchStatus(
        data.results.length
          ? `${data.results.length} channels loaded`
          : 'No channels found',
      );
    } catch (e) {
      setSearchStatus('Failed to load: ' + e.message, true);
    }
  }

  updateResultsVisibility();
  checkImportStatus();
})();
