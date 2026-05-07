(() => {
  const els = {
    q: document.getElementById('q'),
    closeOverlay: document.getElementById('close-overlay'),
    status: document.getElementById('status'),
    grid: document.getElementById('grid'),
    results: document.getElementById('results'),
    player: document.getElementById('player'),
    video: document.getElementById('video'),
    playerStatus: document.getElementById('player-status'),
    playBtn: document.getElementById('play-btn'),
  };

  let currentHls = null;
  let currentId = null;
  let searchSeq = 0;
  let retryCount = 0;
  let stallTimer = null;
  let reloadTimer = null;
  // Backoff schedule (ms) applied before each reload attempt. The first
  // attempt is instant; later attempts wait longer so a slow Ace stream
  // warm-up (peer discovery, ffmpeg respawn) can complete without us
  // hammering it. Total recovery window ~3 minutes.
  const RETRY_DELAYS_MS = [0, 2000, 5000, 10000, 20000, 30000, 45000, 60000];
  const MAX_RETRIES = RETRY_DELAYS_MS.length;
  const STALL_MS = 8000;

  const debounce = (fn, ms) => {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  };

  const setStatus = (msg, isErr = false) => {
    els.status.textContent = msg || '';
    els.status.classList.toggle('err', !!isErr);
  };

  const setPlayerStatus = (msg, isErr = false) => {
    els.playerStatus.textContent = msg || '';
    els.playerStatus.classList.toggle('err', !!isErr);
  };

  // Results overlay is shown when the user is searching OR nothing is playing.
  // Hidden only when a channel is playing AND the search box is empty. The
  // close button is shown whenever the overlay is visible AND a channel is
  // already playing (so closing it has somewhere to go).
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

    if (!q) {
      els.grid.innerHTML = '';
      setStatus('Type a query (e.g. "sport", "bbc", "movie") to search search-ace.stream.');
      return;
    }
    setStatus('Searching upstream source…');
    const params = new URLSearchParams({ q });
    try {
      const r = await fetch('/api/streams?' + params);
      const data = await r.json();
      if (seq !== searchSeq) return;
      render(data.results || []);
      const u = data.upstream;
      const tail = u && u.cached ? ' (cached)' : '';
      setStatus(
        data.results.length
          ? `${data.results.length} channels${tail}`
          : `No matches${tail}`,
      );
    } catch (e) {
      setStatus('Search failed: ' + e.message, true);
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
      card.innerHTML = `
        <div class="name"></div>
        <div class="meta"></div>
      `;
      card.querySelector('.name').textContent = row.name || row.content_id;
      const meta = card.querySelector('.meta');
      if (row.content_id === currentId) {
        const c = document.createElement('span');
        c.className = 'chip now';
        c.textContent = 'Now playing';
        meta.appendChild(c);
      }
      if (row.category) {
        const c = document.createElement('span');
        c.className = 'chip';
        c.textContent = row.category;
        meta.appendChild(c);
      }
      const s = document.createElement('span');
      s.className = 'chip';
      s.textContent = row.source;
      meta.appendChild(s);
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
    if (currentHls) { try { currentHls.destroy(); } catch (e) {} currentHls = null; }
    els.video.pause();
    els.video.removeAttribute('src');
    els.video.load();
    if (currentId) {
      const id = currentId;
      navigator.sendBeacon
        ? navigator.sendBeacon('/api/stop/' + id)
        : fetch('/api/stop/' + id, { method: 'POST', keepalive: true }).catch(() => {});
    }
    currentId = null;
  }

  // Reload the current stream without changing channel. Used when the video
  // element errors, hits EOF on a live stream, or stalls for too long. Uses
  // a backoff schedule so a slow Ace stream warm-up can finish before we
  // give up. The backend's hls.ensure() respawns ffmpeg if its process died.
  function reloadStream(reason) {
    if (!currentId) return;
    if (reloadTimer) return;        // a reload is already queued
    clearStallTimer();
    if (retryCount >= MAX_RETRIES) {
      setPlayerStatus(`Stream keeps stopping (${reason}). Tap the channel again to retry.`, true);
      return;
    }
    const delay = RETRY_DELAYS_MS[retryCount];
    retryCount++;
    const startedFor = currentId;
    const tick = () => {
      const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      if (remaining > 0) {
        setPlayerStatus(`Reconnecting (${reason}, attempt ${retryCount}/${MAX_RETRIES}) in ${remaining}s…`);
      }
    };
    const deadline = Date.now() + delay;
    tick();
    const ticker = setInterval(tick, 1000);

    reloadTimer = setTimeout(() => {
      clearInterval(ticker);
      reloadTimer = null;
      if (currentId !== startedFor) return;     // user switched channel mid-wait
      setPlayerStatus(`Reconnecting (${reason}, attempt ${retryCount}/${MAX_RETRIES})…`);
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

  // Try to play; if the browser blocks autoplay, show a play button overlay
  // so the user can tap to start (a tap counts as a user gesture).
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
    hideOverlay();
    retryCount = 0;
    setPlayerStatus('Resolving stream… (this can take 10–30 seconds)');
    els.playBtn.hidden = true;

    // Mark the card as playing in-place.
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
      hls.on(Hls.Events.ERROR, (_evt, data) => {
        if (data.fatal) setPlayerStatus(`Playback error: ${data.type}/${data.details}`, true);
      });
      currentHls = hls;
    } else {
      setPlayerStatus('Your browser does not support HLS playback.', true);
      return;
    }

    const onPlaying = () => {
      setPlayerStatus('');
      els.video.removeEventListener('playing', onPlaying);
    };
    els.video.addEventListener('playing', onPlaying);
  }

  // Recovery: register once on the video element. These fire for every stream.
  els.video.addEventListener('playing', () => {
    retryCount = 0;
    clearStallTimer();
    clearReloadTimer();
    els.playBtn.hidden = true;
  });
  els.video.addEventListener('error', () => reloadStream('error'));
  // Live HLS shouldn't naturally end; if it does, the source bailed.
  els.video.addEventListener('ended', () => reloadStream('ended'));
  // 'waiting' fires whenever the buffer underruns. Give iOS some grace, then
  // reload if playback hasn't resumed.
  els.video.addEventListener('waiting', () => {
    clearStallTimer();
    stallTimer = setTimeout(() => {
      if (els.video.paused) return;            // user paused intentionally
      if (els.video.readyState >= 3) return;   // recovered on its own
      reloadStream('stall');
    }, STALL_MS);
  });
  els.video.addEventListener('stalled', () => {
    // Same handling as 'waiting' — the iOS event is sometimes 'stalled' instead.
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

  // Tapping anywhere on the overlay outside a card closes it.
  els.results.addEventListener('click', (e) => {
    if (e.target.closest('.card')) return;
    hideOverlay();
  });

  els.q.addEventListener('input', debounce(search, 250));
  // Tapping the search input always reveals the overlay, so the user can
  // pick a different channel mid-playback even with no query typed.
  els.q.addEventListener('focus', () => {
    els.results.hidden = false;
    if (currentId) els.closeOverlay.hidden = false;
  });
  els.closeOverlay.addEventListener('click', () => {
    els.q.blur();
    hideOverlay();
  });
  window.addEventListener('pagehide', stopCurrent);

  updateResultsVisibility();
  search();
})();
