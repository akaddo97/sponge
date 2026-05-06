// === S1: JAZZ INTEGRATION ===
// Sibling to sponge_home.js — drives /jazz route only. Never touches the
// canonical sponge_home.js or its surfaces. Contract:
//   • Container transform on mic-tap (toggles body.jazz-chat-active).
//   • Submit handler streams /api/query/mobile (same SSE shape as Sub-05).
//   • Press-and-hold mic uploads to /api/inbox/audio_upload.
//   • Stats footer subscribes to /api/sponge/terminal/feed (Sub-13).
//   • Voice pending stream from /api/voice/pending (Agent C / Sub-10).
//   • Inline graph cards consume `mobile_card` SSE events (Sub-01).
(() => {
  const root = document.querySelector('[data-jazz="root"]');
  if (!root) return;  // not on /jazz page

  // ── DOM helpers ────────────────────────────────────────────────────────────
  const $  = (sel, where = document) => where.querySelector(sel);
  const $$ = (sel, where = document) => Array.from(where.querySelectorAll(sel));

  function escCh(s) {
    return String(s == null ? "" : s).replace(
      /[&<>"']/g,
      c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])
    );
  }

  // ── Container transform ────────────────────────────────────────────────────
  function openChat() {
    document.body.classList.add('jazz-chat-active');
    const surface = $('#jazz-chat-surface');
    if (surface) surface.setAttribute('aria-hidden', 'false');
    setTimeout(() => $('#jazz-search-input')?.focus(), 320);
  }
  function closeChat() {
    document.body.classList.remove('jazz-chat-active');
    const surface = $('#jazz-chat-surface');
    if (surface) surface.setAttribute('aria-hidden', 'true');
  }

  // ── Dashboard population ──────────────────────────────────────────────────
  async function refreshDashboard() {
    try {
      const r = await fetch('/api/sponge/dashboard');
      if (!r.ok) return;
      const d = await r.json();

      const ws = $('[data-widget="graph-size"]');
      if (ws) {
        const nodes = d.graph?.nodes ?? 0;
        const edges = d.graph?.edges ?? 0;
        $('[data-field="nodes"]', ws).textContent = nodes;
        $('[data-field="sub"]', ws).textContent = `${nodes} nodes · ${edges} edges`;
        const ratio = Math.min(1, nodes / 1000);
        $('[data-field="bar"]', ws).style.width = `${Math.round(ratio * 100)}%`;
      }

      const wk = $('[data-widget="this-week"]');
      if (wk) {
        const unvNodes = d.unverified?.nodes ?? 0;
        const unvEdges = d.unverified?.edges ?? 0;
        $('[data-field="count"]', wk).textContent = unvNodes + unvEdges;
        $('[data-field="sub"]', wk).textContent =
          `${unvNodes} provisional nodes · ${unvEdges} edges`;
      }
    } catch { /* offline; widgets keep placeholders */ }
  }

  async function refreshTopConnections() {
    try {
      const r = await fetch('/api/sponge/topics');
      if (!r.ok) return;
      const d = await r.json();
      const w = $('[data-widget="top-connections"]');
      if (!w) return;
      const list = $('[data-field="pills"]', w);
      list.innerHTML = '';
      const topics = (d.topics || []).slice(0, 3);
      if (topics.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'jazz-node-pill';
        empty.innerHTML = `<div class="jazz-node-pill-dot"></div>—`;
        list.appendChild(empty);
        return;
      }
      topics.forEach(t => {
        const pill = document.createElement('div');
        pill.className = 'jazz-node-pill';
        pill.innerHTML = `<div class="jazz-node-pill-dot"></div>${escCh(t.label || '')}`;
        if (t.prefill_query) {
          pill.addEventListener('click', () => {
            openChat();
            const input = $('#jazz-search-input');
            if (input) input.value = t.prefill_query;
          });
        }
        list.appendChild(pill);
      });
    } catch { /* topics endpoint may be rate-limited; ignore */ }
  }

  // ── Stats footer (Sub-13 SSE) ─────────────────────────────────────────────
  let statsES = null;
  let statsRetryT = null;
  function startStatsFooter() {
    const target = $('.jazz-stats-text', $('#jazz-stats-footer'));
    if (!target || typeof EventSource === 'undefined') return;

    const stats = {
      nodes: '—',
      edges: '—',
      validator: '—',
      lastIngest: '—',
    };
    const compose = () => {
      target.textContent =
        `NODES ${stats.nodes} · EDGES ${stats.edges} · ` +
        `VALIDATOR ${String(stats.validator).toUpperCase()} · ` +
        `LAST INGEST ${stats.lastIngest} · GPU NONE · SERVER NONE · HOST localhost`;
    };

    statsES = new EventSource('/api/sponge/terminal/feed');
    statsES.onmessage = (ev) => {
      let payload; try { payload = JSON.parse(ev.data); } catch { return; }
      const line = String(payload.line || '');
      const validatorM = line.match(/\[validator\]\s+(\w+)\s+—\s+(\d+)\s+nodes,\s+(\d+)\s+edges/);
      if (validatorM) {
        stats.validator = validatorM[1];
        stats.nodes = validatorM[2];
        stats.edges = validatorM[3];
      }
      const ingestM = line.match(/\[ingest\]\s+(.+)/);
      if (ingestM) {
        const summary = ingestM[1].trim();
        stats.lastIngest = summary.slice(0, 60);
        const w = $('[data-widget="last-ingest"]');
        if (w) {
          $('[data-field="name"]', w).textContent = summary.slice(0, 24);
          $('[data-field="sub"]', w).textContent = 'live · sponge feed';
        }
      }
      // === M3: REALTIME CARDS ===
      // Same SSE stream; render an inline card if line is a graph-add ingest.
      // parseGraphAddLine is forward-compatible — when Sub-13 carries node_id /
      // label / file_type, the card upgrades automatically (no JS rework).
      const m3Payload = parseGraphAddLine(line);
      if (m3Payload) renderGraphAddCard(m3Payload);
      // === END M3 ===
      compose();
    };
    statsES.onerror = () => {
      try { statsES.close(); } catch {}
      clearTimeout(statsRetryT);
      statsRetryT = setTimeout(startStatsFooter, 5000);
    };
  }

  // === M3: REALTIME CARDS ===
  // When a voice memo (or any commit_provisional path) lands new graph nodes,
  // Sub-13's terminal feed emits a line of the form:
  //   [ingest]  ok — +N nodes, +M edges | <provisional_source> | <ts>
  // We parse that line, render an inline card in the chat scroll area, and
  // dedup by (node_id || provisional_source) within a 30s window. When the
  // home zone is the active surface, a compact toast above the dashboard grid
  // links into the chat surface where the inline card lives.
  //
  // Sub-13 currently does NOT carry node_id / label / file_type — see the M3
  // Done block in docs/orchestration/2026-05-04_mvp_voice_loop/03_realtime_graph_cards.md.
  // parseGraphAddLine is written so that adding `| node=<id>:<label>:<file_type>`
  // (or another structured suffix) to the line will upgrade the card with no
  // further frontend work.

  const M3_DEDUP_WINDOW_MS = 30_000;
  // Page-load gate: events with timestamps older than (pageLoadTs - 5s) are
  // backlog from Sub-13's snapshot replay, not live activity. Skip them so
  // refreshing the page doesn't toast yesterday's ingest every time.
  const M3_PAGE_LOAD_TS = Date.now();
  const M3_BACKLOG_GRACE_MS = 5_000;
  const m3RecentCards = new Map();   // key -> {cardEl, ts, nodes, edges}
  const m3ToastTimers = new Map();   // key -> timeoutId for auto-dismiss

  function parseGraphAddLine(line) {
    // Sub-13 emits: [ingest]  ok — +N nodes, +M edges | <provisional_source> [| <ts>] [| node=...] [| nodes=[...]] [| edges_to=...]
    // The trailing `|` segments are optional and can appear in any order. Edge-only
    // ingests (N=0) are valid IFF an `edges_to=` segment supplies a subject — see
    // the === M3 MULTI === block below.
    const m = String(line || '').match(
      /\[ingest\]\s+(\w+)\s+—\s+\+(\d+)\s+nodes,\s+\+(\d+)\s+edges\s+\|\s+(.+)$/
    );
    if (!m) return null;
    const status = m[1];
    const nodesAdded = parseInt(m[2], 10);
    const edgesAdded = parseInt(m[3], 10);
    if (status !== 'ok') return null;
    const segments = m[4].split(/\s+\|\s+/).map(s => s.trim()).filter(Boolean);
    const provisionalSource = segments.shift() || '';
    let timestamp = '';
    let nodeId = null;
    let label = null;
    let fileType = null;
    // === M3 MULTI ===
    let multiNodes = null;
    let edgeSubject = null;
    // === END M3 MULTI ===
    for (const seg of segments) {
      if (seg.startsWith('node=')) {
        const nodeM = seg.slice(5).match(/^([^:]+):([^:]+):(.+)$/);
        if (nodeM) {
          nodeId = nodeM[1];
          label = nodeM[2];
          fileType = nodeM[3];
        }
      } else if (seg.startsWith('nodes=[') && seg.endsWith(']')) {
        // === M3 MULTI === nodes=[id1:label1:type1, id2:label2:type2, ...]
        const inner = seg.slice(7, -1);
        const items = inner.split(/,\s*/).map(item => {
          const im = item.match(/^([^:]+):([^:]+):(.+)$/);
          return im ? { id: im[1], label: im[2], file_type: im[3] } : null;
        }).filter(Boolean);
        if (items.length) multiNodes = items;
      } else if (seg.startsWith('edges_to=')) {
        // === M3 MULTI === edges_to=<subject_id>:<subject_label>
        const em = seg.slice(9).match(/^([^:]+):(.+)$/);
        if (em) edgeSubject = { id: em[1], label: em[2] };
      } else {
        timestamp = seg;
      }
    }
    // A line is a graph-add iff it carries at least one new node OR a real edge subject.
    if (nodesAdded < 1 && !edgeSubject) return null;
    return {
      nodes: nodesAdded,
      edges: edgesAdded,
      provisional_source: provisionalSource,
      timestamp,
      node_id: nodeId,
      label,
      file_type: fileType,
      multi_nodes: multiNodes,
      edge_subject: edgeSubject,
    };
  }

  function _m3IsBacklog(payload) {
    if (!payload.timestamp) return false;     // no ts → assume live
    const eventTs = Date.parse(payload.timestamp);
    if (!Number.isFinite(eventTs)) return false;
    return eventTs < M3_PAGE_LOAD_TS - M3_BACKLOG_GRACE_MS;
  }

  function _m3DedupKey(payload) {
    return payload.node_id || payload.provisional_source || `unknown_${Date.now()}`;
  }

  // === M3 MULTI ===
  // Card-shape resolution priority:
  //   1. payload.label              → 'single' (existing behaviour)
  //   2. payload.multi_nodes        → 'multi'
  //   3. payload.edge_subject       → 'edge'
  //   4. fallback                   → 'generic'
  // Slug fallback (e.g. raw provisional_source) is never used as the title —
  // the generic shape produces "+N graph adds" instead.
  function _m3CardShape(payload) {
    if (payload.label) return 'single';
    if (Array.isArray(payload.multi_nodes) && payload.multi_nodes.length) return 'multi';
    if (payload.edge_subject && payload.edge_subject.label) return 'edge';
    return 'generic';
  }

  function _m3CardTitle(payload) {
    const shape = _m3CardShape(payload);
    if (shape === 'single') return payload.label;
    if (shape === 'multi') {
      const n = payload.nodes;
      return `+${n} node${n === 1 ? '' : 's'}`;
    }
    if (shape === 'edge') {
      const n = payload.edges;
      return `+${n} edge${n === 1 ? '' : 's'} to ${payload.edge_subject.label}`;
    }
    const total = (payload.nodes || 0) + (payload.edges || 0);
    return `+${total} graph add${total === 1 ? '' : 's'}`;
  }
  // === END M3 MULTI ===

  function _m3FormatSubtitle(payload) {
    // === M3 MULTI ===
    const shape = _m3CardShape(payload);
    if (shape === 'multi') {
      const labels = payload.multi_nodes.map(n => n.label);
      const visible = labels.slice(0, 3).join(', ');
      const more = labels.length > 3 ? ` (and ${labels.length - 3} more)` : '';
      const tail = [];
      if (payload.provisional_source) tail.push(payload.provisional_source);
      if (payload.timestamp) tail.push(payload.timestamp);
      return tail.length ? `${visible}${more} · ${tail.join(' · ')}` : `${visible}${more}`;
    }
    // === END M3 MULTI ===
    const parts = [];
    if (payload.file_type) parts.push(payload.file_type);
    if (payload.provisional_source) parts.push(payload.provisional_source);
    if (payload.timestamp) parts.push(payload.timestamp);
    return parts.join(' · ');
  }

  function _m3VerifyHref(payload) {
    // Only single-node payloads can deep-link into /verify?focus=, since the
    // unverified-node modal only loads one id at a time. Multi/edge/generic
    // shapes go to bare /verify.
    return payload.node_id
      ? `/verify?focus=${encodeURIComponent(payload.node_id)}`
      : '/verify';
  }

  function _m3MetaText(nodes, edges) {
    return `+${nodes} node${nodes === 1 ? '' : 's'}, +${edges} edge${edges === 1 ? '' : 's'}`;
  }

  function _m3BuildCardElement(payload) {
    const row = document.createElement('div');
    row.className = 'jazz-bubble-row jazz-graph-add-card-row';
    row.dataset.m3CardKey = _m3DedupKey(payload);
    // === M3 MULTI === card-shape attribute for tests + future styling hooks.
    row.dataset.cardShape = _m3CardShape(payload);

    const card = document.createElement('div');
    card.className = 'jazz-graph-add-card';
    card.dataset.cardShape = row.dataset.cardShape;
    // === END M3 MULTI ===
    card.style.cssText = [
      'border:1px solid var(--sp-yellow,#facc15)',
      'background:var(--sp-card,#fff)',
      'border-radius:12px',
      'padding:12px 14px',
      'margin:6px 0',
    ].join(';');

    const meta = document.createElement('div');
    meta.dataset.field = 'meta';
    meta.className = 'jazz-graph-add-card-meta';
    meta.style.cssText = 'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;opacity:.65;letter-spacing:.04em';
    meta.textContent = _m3MetaText(payload.nodes, payload.edges);

    const name = document.createElement('div');
    name.dataset.field = 'name';
    name.className = 'jazz-graph-add-card-name';
    name.style.cssText = 'font-size:16px;font-weight:600;margin-top:4px';
    // === M3 MULTI === Title via shape-aware resolver — never the slug.
    name.textContent = _m3CardTitle(payload);
    // === END M3 MULTI ===

    const sub = document.createElement('div');
    sub.dataset.field = 'sub';
    sub.className = 'jazz-graph-add-card-sub';
    sub.style.cssText = 'font-size:12px;opacity:.7;margin-top:2px';
    sub.textContent = _m3FormatSubtitle(payload);

    const actions = document.createElement('div');
    actions.className = 'jazz-graph-add-card-actions';
    actions.style.cssText = 'display:flex;gap:8px;margin-top:10px';

    const verify = document.createElement('a');
    verify.className = 'jazz-graph-add-card-verify';
    verify.href = _m3VerifyHref(payload);
    verify.textContent = 'Verify';
    verify.style.cssText = [
      'background:var(--sp-yellow,#facc15)',
      'color:#000',
      'padding:6px 12px',
      'border-radius:6px',
      'font-size:13px',
      'font-weight:600',
      'text-decoration:none',
    ].join(';');

    const later = document.createElement('button');
    later.type = 'button';
    later.className = 'jazz-graph-add-card-later';
    later.textContent = 'Later';
    later.style.cssText = [
      'background:#e5e5e5',
      'color:#333',
      'padding:6px 12px',
      'border-radius:6px',
      'font-size:13px',
      'border:none',
      'cursor:pointer',
    ].join(';');
    later.addEventListener('click', () => {
      row.remove();
      m3RecentCards.delete(row.dataset.m3CardKey);
    });

    actions.appendChild(verify);
    actions.appendChild(later);
    card.appendChild(meta);
    card.appendChild(name);
    card.appendChild(sub);
    card.appendChild(actions);
    row.appendChild(card);
    return row;
  }

  function _m3InsertCard(rowEl) {
    const area = $('#jazz-chat-area');
    if (!area) return false;
    area.appendChild(rowEl);
    if (document.body.classList.contains('jazz-chat-active')) {
      area.scrollTop = area.scrollHeight;
    }
    return true;
  }

  function _m3ShowToast(payload) {
    if (document.body.classList.contains('jazz-chat-active')) return;
    const key = _m3DedupKey(payload);
    let toast = document.querySelector(`[data-m3-toast-key="${CSS.escape(key)}"]`);
    if (!toast) {
      toast = document.createElement('button');
      toast.type = 'button';
      toast.className = 'jazz-graph-add-toast';
      toast.dataset.m3ToastKey = key;
      toast.style.cssText = [
        'position:fixed',
        'top:8px',
        'left:50%',
        'transform:translateX(-50%)',
        'background:var(--sp-yellow,#facc15)',
        'color:#000',
        'border:none',
        'padding:8px 14px',
        'border-radius:999px',
        'font-size:13px',
        'font-weight:600',
        'box-shadow:0 2px 8px rgba(0,0,0,.15)',
        'cursor:pointer',
        'z-index:9999',
      ].join(';');
      document.body.appendChild(toast);
      toast.addEventListener('click', () => {
        openChat();
        const card = document.querySelector(`[data-m3-card-key="${CSS.escape(key)}"]`);
        if (card) setTimeout(() => card.scrollIntoView({behavior:'smooth', block:'center'}), 320);
        toast.remove();
        m3ToastTimers.delete(key);
      });
    }
    // === M3 MULTI === Toast noun via shape-aware resolver — never the slug.
    toast.textContent = `${_m3CardTitle(payload)} added →`;
    // === END M3 MULTI ===
    clearTimeout(m3ToastTimers.get(key));
    const timer = setTimeout(() => {
      toast.remove();
      m3ToastTimers.delete(key);
    }, 8000);
    m3ToastTimers.set(key, timer);
  }

  function renderGraphAddCard(payload) {
    if (!payload || typeof payload !== 'object') return null;
    if (_m3IsBacklog(payload)) return null;     // page-load gate
    const key = _m3DedupKey(payload);
    const now = Date.now();
    for (const [k, v] of m3RecentCards) {
      if (now - v.ts > M3_DEDUP_WINDOW_MS) m3RecentCards.delete(k);
    }
    const existing = m3RecentCards.get(key);
    if (existing && now - existing.ts <= M3_DEDUP_WINDOW_MS && existing.cardEl?.isConnected) {
      existing.edges += payload.edges;
      existing.nodes += payload.nodes;
      existing.ts = now;
      const meta = existing.cardEl.querySelector('[data-field="meta"]');
      if (meta) meta.textContent = _m3MetaText(existing.nodes, existing.edges);
      _m3ShowToast(payload);
      return existing.cardEl;
    }
    const rowEl = _m3BuildCardElement(payload);
    _m3InsertCard(rowEl);
    m3RecentCards.set(key, { cardEl: rowEl, ts: now, nodes: payload.nodes, edges: payload.edges });
    _m3ShowToast(payload);
    return rowEl;
  }

  // Expose only for tests / debugging — IIFE encapsulation otherwise
  if (typeof window !== 'undefined') {
    window.__M3 = {
      parseGraphAddLine,
      renderGraphAddCard,
      m3RecentCards,
      M3_DEDUP_WINDOW_MS,
      // === M3 MULTI === resolvers for shape + title — used by tests
      _cardShape: _m3CardShape,
      _cardTitle: _m3CardTitle,
      // === END M3 MULTI ===
    };
  }
  // === END M3 ===

  // ── Bubble factory ────────────────────────────────────────────────────────
  function makeBubble(variant, text) {
    const t = $('#jazz-bubble-template');
    if (!t) return null;
    const node = t.content.cloneNode(true);
    const row = node.querySelector('.jazz-bubble-row');
    const bubble = node.querySelector('.jazz-bubble');
    if (variant === 'sent' || variant === 'user') {
      row.classList.add('jazz-bubble-row-sent');
      bubble.classList.add('jazz-bubble-sent');
      bubble.dataset.variant = 'sent';
    } else if (variant === 'recv' || variant === 'assistant') {
      bubble.classList.add('jazz-bubble-recv');
      bubble.dataset.variant = 'recv';
    } else {
      bubble.classList.add('jazz-bubble-recv');
      bubble.dataset.variant = 'status';
    }
    if (text) bubble.querySelector('.jazz-bubble-body').textContent = text;
    return row;
  }

  function appendBubble(rowEl) {
    const area = $('#jazz-chat-area');
    if (!area || !rowEl) return null;
    area.appendChild(rowEl);
    area.scrollTop = area.scrollHeight;
    return rowEl;
  }

  // ── INLINE VERIFY CARD ────────────────────────────────────────────────────
  // After audio_upload commits provisional nodes/edges, render a card-bubble
  // listing each mutation with ✓/✗ buttons. Click ✓ → /api/verify/apply,
  // ✗ → /api/verify/reject. Card swaps to a locked summary on success.
  // Backend ships proposal.edges with source_label / target_label already
  // resolved against the live graph so this layer is pure rendering.
  function makeVerifyCardBubble(commitSummary, proposal) {
    const t = $('#jazz-verify-card-template');
    if (!t) return null;
    const node = t.content.cloneNode(true);
    const row = node.querySelector('.jazz-bubble-row');
    const card = node.querySelector('.jazz-verify-card');
    const list = node.querySelector('[data-field="list"]');
    const status = node.querySelector('[data-field="status"]');
    if (!card || !list) return null;

    const source = commitSummary && commitSummary.source;
    if (!source) return null;
    card.dataset.source = source;

    const nodes = (proposal && proposal.nodes) || [];
    const edges = (proposal && proposal.edges) || [];
    if (!nodes.length && !edges.length) return null;

    nodes.forEach(n => {
      const item = document.createElement('div');
      item.className = 'jazz-verify-item';
      item.innerHTML = `<span class="jazz-verify-kind">+ ${escCh(n.file_type || 'node')}</span> · <span class="jazz-verify-label">${escCh(n.label || n.id || '?')}</span>`;
      list.appendChild(item);
    });
    edges.forEach(e => {
      const item = document.createElement('div');
      item.className = 'jazz-verify-item';
      const src = e.source_label || e.source || '?';
      const tgt = e.target_label || e.target || '?';
      const rel = (e.relation || 'related_to').replace(/_/g, ' ');
      item.innerHTML = `<span class="jazz-verify-kind">+ edge</span> · <span class="jazz-verify-label">${escCh(src)}</span> <span class="jazz-verify-rel">— ${escCh(rel)} →</span> <span class="jazz-verify-label">${escCh(tgt)}</span>`;
      list.appendChild(item);
    });

    const lockCard = (state, msg) => {
      card.dataset.state = state;
      card.querySelectorAll('.jazz-verify-btn').forEach(b => { b.disabled = true; });
      if (msg) {
        status.textContent = msg;
        status.hidden = false;
      }
    };

    card.querySelectorAll('.jazz-verify-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        const action = btn.dataset.action;
        if (!action) return;
        const path = action === 'apply' ? '/api/verify/apply' : '/api/verify/reject';
        // Mid-call locked state — stop double-clicks
        card.querySelectorAll('.jazz-verify-btn').forEach(b => { b.disabled = true; });
        try {
          const r = await fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provisional_source: source }),
          });
          const data = await r.json().catch(() => ({}));
          if (!r.ok || !data.ok) {
            lockCard('error', `Error: ${data.error || 'HTTP ' + r.status}`);
            // Re-enable so the user can retry
            card.querySelectorAll('.jazz-verify-btn').forEach(b => { b.disabled = false; });
            card.dataset.state = 'proposed';
            return;
          }
          if (action === 'apply') {
            const n = data.flipped || (nodes.length + edges.length);
            lockCard('verified', `✓ Verified — ${n} item${n === 1 ? '' : 's'} committed to graph`);
          } else {
            const n = data.removed || (nodes.length + edges.length);
            lockCard('rejected', `✗ Discarded — ${n} item${n === 1 ? '' : 's'} dropped`);
          }
        } catch (err) {
          lockCard('error', `Network error: ${err.message}`);
          card.querySelectorAll('.jazz-verify-btn').forEach(b => { b.disabled = false; });
          card.dataset.state = 'proposed';
        }
      });
    });

    return row;
  }

  // Lightweight HTML-escape — only for short labels coming from the graph.
  function escCh(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function makeCardBubble(card) {
    const t = $('#jazz-card-template');
    if (!t) return null;
    const node = t.content.cloneNode(true);
    const row = node.querySelector('.jazz-bubble-row');
    const name = node.querySelector('.jazz-graph-card-name');
    const sub = node.querySelector('.jazz-graph-card-sub');
    const tags = node.querySelector('.jazz-graph-card-tags');
    const link = node.querySelector('.jazz-graph-card-link');
    if (name) name.textContent = card.title || '';
    if (sub) sub.textContent = card.subtitle || '';
    if (tags && Array.isArray(card.tags)) {
      card.tags.forEach(tag => {
        const t2 = document.createElement('span');
        t2.className = 'jazz-graph-tag';
        t2.textContent = tag;
        tags.appendChild(t2);
      });
    }
    if (link && card.node_id) {
      link.href = `/brief/${encodeURIComponent(card.node_id)}`;
    }
    return row;
  }

  // ── Strip markdown for text-message-style rendering ───────────────────────
  // Chat output should read like a text from a friend, not a markdown
  // document. Strips the four formats that LLMs default to (**bold**, *italic*,
  // _italic_, `code`) plus leading # headers. Non-destructive — leaves URLs,
  // punctuation, emoji.
  function stripMarkdown(text) {
    return String(text || '')
      .replace(/\*\*([^*]+?)\*\*/g, '$1')
      .replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, '$1')
      .replace(/(?<!_)_([^_\n]+?)_(?!_)/g, '$1')
      .replace(/`([^`\n]+?)`/g, '$1')
      .replace(/^#{1,6}\s+/gm, '');
  }

  // ── /api/query/mobile SSE consumer ────────────────────────────────────────
  async function streamQuery(message) {
    const asstRow = appendBubble(makeBubble('recv', ''));
    if (!asstRow) return;
    const body = asstRow.querySelector('.jazz-bubble-body');
    let finalText = '';
    try {
      const resp = await fetch('/api/query/mobile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, history: [] }),
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          let evt; try { evt = JSON.parse(line.slice(6)); } catch { continue; }
          if (evt.type === 'status') {
            body.innerHTML = `<span class="jazz-bubble-status">${escCh(evt.text || '')}</span>`;
          } else if (evt.type === 'tool') {
            // Drop the "Using" prefix — the label already starts with a verb
            // (e.g. "Searching for Taslim") so prefixing read as "Using Searching for Taslim".
            body.innerHTML = `<span class="jazz-bubble-status">${escCh(evt.label || 'thinking')}…</span>`;
          } else if (evt.type === 'text') {
            finalText = evt.content || '';
            body.textContent = stripMarkdown(finalText);
          } else if (evt.type === 'mobile_card') {
            appendBubble(makeCardBubble(evt));
          } else if (evt.type === 'provisional') {
            const n = evt.nodes, e = evt.edges;
            const footer = document.createElement('div');
            footer.className = 'jazz-bubble-status';
            footer.style.marginTop = '6px';
            footer.innerHTML = `${n} provisional node${n === 1 ? '' : 's'}, ${e} edge${e === 1 ? '' : 's'} — <a class="jazz-graph-card-link" style="display:inline" href="/verify">verify</a>`;
            asstRow.querySelector('.jazz-bubble').appendChild(footer);
          } else if (evt.type === 'error') {
            body.innerHTML = `<span class="jazz-bubble-error">Error: ${escCh(evt.message || '')}</span>`;
          }
          const area = $('#jazz-chat-area');
          if (area) area.scrollTop = area.scrollHeight;
        }
      }
    } catch (err) {
      body.innerHTML = `<span class="jazz-bubble-error">Network error: ${escCh(err.message || '')}</span>`;
    }
  }

  // ── Submit handler ────────────────────────────────────────────────────────
  function wireSubmit() {
    const form = $('#jazz-search-form');
    const input = $('#jazz-search-input');
    if (!form || !input) return;
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const msg = (input.value || '').trim();
      if (!msg) return;
      openChat();
      appendBubble(makeBubble('sent', msg));
      input.value = '';
      streamQuery(msg);
    });
    const sendBtn = $('#jazz-input-send-btn');
    if (sendBtn) {
      sendBtn.addEventListener('click', () => {
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.dispatchEvent(new Event('submit', { cancelable: true }));
      });
    }
  }

  // === M4: TAP-TO-TOGGLE MIC ===
  // Replaces the press-and-hold model. Tap to start; tap again to stop+upload.
  // Single global state machine drives both #jazz-mic-button and
  // #jazz-input-mic-btn — they're two facets of one conceptual mic.
  //
  //   states: idle | recording | uploading | error
  //   events: tap, autoStop, cancel, uploadResolve, uploadReject
  //
  // Constraints:
  //   • ≥1s of recording before a second tap is honoured (avoids accidental
  //     no-audio uploads).
  //   • Hard cap at 5:00 — warning banner at 4:50, auto-stop at 5:00.
  //   • Cancel via the X button on the timer banner discards audio without
  //     uploading.
  //   • /api/inbox/audio_upload contract unchanged (Sub-10).
  const MIC_STATES = Object.freeze({
    IDLE: 'idle',
    RECORDING: 'recording',
    UPLOADING: 'uploading',
    ERROR: 'error',
  });
  const MIN_RECORD_MS = 1000;
  const WARNING_AT_MS = 290000;       // 4:50
  const AUTO_STOP_AT_MS = 300000;     // 5:00 hard cap = 5 * 60 * 1000
  const TIMER_TICK_MS = 250;

  let micState = MIC_STATES.IDLE;
  let mediaRecorder = null;
  let audioChunks = [];
  let recordStartTs = 0;
  let timerIntervalId = null;
  let warningTimeoutId = null;
  let autoStopTimeoutId = null;
  let uploadCancelled = false;

  function micButtons() {
    return [$('#jazz-mic-button'), $('#jazz-input-mic-btn')].filter(Boolean);
  }

  function ensureTimerBanner() {
    let banner = $('#jazz-recording-banner');
    if (banner) return banner;
    banner = document.createElement('div');
    banner.id = 'jazz-recording-banner';
    banner.className = 'jazz-recording-banner';
    banner.setAttribute('aria-hidden', 'true');
    banner.innerHTML = `
      <span class="jazz-recording-dot" aria-hidden="true"></span>
      <span class="jazz-recording-timer" data-field="elapsed">0:00</span>
      <span class="jazz-recording-warning" data-field="warning" hidden>Recording will stop in 10s</span>
      <button type="button" class="jazz-recording-cancel" aria-label="Cancel recording">×</button>
    `;
    const surface = $('#jazz-chat-surface') || document.body;
    surface.appendChild(banner);
    banner.querySelector('.jazz-recording-cancel').addEventListener('click', (e) => {
      e.stopPropagation();
      cancelRecording();
    });
    return banner;
  }

  function showRecordingBanner() {
    const banner = ensureTimerBanner();
    banner.classList.add('jazz-recording-banner-active');
    banner.setAttribute('aria-hidden', 'false');
    banner.querySelector('[data-field="warning"]').hidden = true;
    updateTimerLabel(0);
  }

  function hideRecordingBanner() {
    const banner = $('#jazz-recording-banner');
    if (!banner) return;
    banner.classList.remove('jazz-recording-banner-active');
    banner.setAttribute('aria-hidden', 'true');
    banner.querySelector('[data-field="warning"]').hidden = true;
  }

  function updateTimerLabel(elapsedMs) {
    const banner = $('#jazz-recording-banner');
    if (!banner) return;
    const totalSec = Math.floor(elapsedMs / 1000);
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    banner.querySelector('[data-field="elapsed"]').textContent =
      `${m}:${String(s).padStart(2, '0')}`;
  }

  function showStoppingSoonWarning() {
    const banner = $('#jazz-recording-banner');
    if (!banner) return;
    banner.querySelector('[data-field="warning"]').hidden = false;
  }

  function applyMicVisual(state) {
    micButtons().forEach(btn => {
      btn.classList.remove('recording', 'uploading');
      // Keep legacy class name for backwards compatibility with any
      // observer / CSS not yet migrated.
      btn.classList.remove('jazz-mic-recording');
      if (state === MIC_STATES.RECORDING) {
        btn.classList.add('recording', 'jazz-mic-recording');
        btn.setAttribute('aria-pressed', 'true');
      } else if (state === MIC_STATES.UPLOADING) {
        btn.classList.add('uploading');
        btn.setAttribute('aria-pressed', 'false');
      } else {
        btn.setAttribute('aria-pressed', 'false');
      }
    });
  }

  function transitionMic(next) {
    micState = next;
    applyMicVisual(next);
    if (next === MIC_STATES.RECORDING) {
      showRecordingBanner();
    } else {
      hideRecordingBanner();
    }
  }

  function clearRecordingTimers() {
    if (timerIntervalId !== null) {
      clearInterval(timerIntervalId);
      timerIntervalId = null;
    }
    if (warningTimeoutId !== null) {
      clearTimeout(warningTimeoutId);
      warningTimeoutId = null;
    }
    if (autoStopTimeoutId !== null) {
      clearTimeout(autoStopTimeoutId);
      autoStopTimeoutId = null;
    }
  }

  async function handleMicTap() {
    if (micState === MIC_STATES.IDLE) {
      await startRecording();
      return;
    }
    if (micState === MIC_STATES.RECORDING) {
      const elapsed = Date.now() - recordStartTs;
      if (elapsed < MIN_RECORD_MS) return;       // ignore too-fast double tap
      await stopAndUpload();
      return;
    }
    // UPLOADING / ERROR — taps are ignored
  }

  async function startRecording() {
    if (micState !== MIC_STATES.IDLE) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      // Fix: the chat surface is hidden on the home zone, so a bubble would
      // be invisible. Open chat first so the user actually sees the message.
      openChat();
      showStatus('Microphone unavailable. Safari needs HTTPS — start Tailscale Funnel ("tailscale funnel 5050") and reload.');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioChunks = [];
      uploadCancelled = false;
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (e) => { if (e.data.size) audioChunks.push(e.data); };
      mediaRecorder.start();
      recordStartTs = Date.now();
      transitionMic(MIC_STATES.RECORDING);
      openChat();
      // Tick the timer and arm the warning + auto-stop.
      timerIntervalId = setInterval(() => {
        updateTimerLabel(Date.now() - recordStartTs);
      }, TIMER_TICK_MS);
      warningTimeoutId = setTimeout(showStoppingSoonWarning, WARNING_AT_MS);
      autoStopTimeoutId = setTimeout(() => {
        if (micState === MIC_STATES.RECORDING) stopAndUpload();
      }, AUTO_STOP_AT_MS);
    } catch {
      transitionMic(MIC_STATES.IDLE);
      openChat();
      showStatus('Enable mic in Safari/browser settings.');
    }
  }

  async function stopAndUpload() {
    if (micState !== MIC_STATES.RECORDING || !mediaRecorder) return;
    clearRecordingTimers();
    transitionMic(MIC_STATES.UPLOADING);
    return new Promise((resolve) => {
      const recorder = mediaRecorder;
      recorder.onstop = async () => {
        const mime = recorder.mimeType || 'audio/webm';
        const blob = new Blob(audioChunks, { type: mime });
        recorder.stream.getTracks().forEach(t => t.stop());
        mediaRecorder = null;
        audioChunks = [];
        if (!uploadCancelled) {
          await uploadAudio(blob);
        }
        transitionMic(MIC_STATES.IDLE);
        resolve();
      };
      try { recorder.stop(); } catch { transitionMic(MIC_STATES.IDLE); resolve(); }
    });
  }

  function cancelRecording() {
    if (micState !== MIC_STATES.RECORDING || !mediaRecorder) return;
    uploadCancelled = true;
    clearRecordingTimers();
    // Drive through the same stop path so MediaRecorder cleanup happens
    // exactly once; uploadCancelled gates the network call.
    stopAndUpload();
  }

  // === M5: BRIEFER ===
  // Pending IDs that uploadAudio has already rendered briefer bubbles for.
  // The /api/voice/pending SSE may also fire for the same memo (race: SSE
  // polls every 2s; upload+briefer takes 1-3s). The handler below skips any
  // memo we've already handled here so we don't double-render the user
  // bubble + double-fire streamQuery.
  const __M5_BRIEFER_HANDLED = new Set();
  // === END M5: BRIEFER ===

  async function uploadAudio(blob) {
    const form = new FormData();
    form.append('file', blob, 'voice_memo.webm');
    form.append('route', 'chat');
    const _doUpload = () => fetch('/api/inbox/audio_upload', { method: 'POST', body: form });
    try {
      let r = await _doUpload();
      if (!r.ok && r.status >= 500) {
        await new Promise(res => setTimeout(res, 800));
        r = await _doUpload();
      }
      if (!r.ok) {
        showStatus(`Upload failed (HTTP ${r.status}).`);
        return;
      }
      // Empty-audio guard (route returns skipped_pipeline=true when Whisper
      // got silence). Show a quiet ephemeral status instead of the cleaner's
      // confused reply that used to appear here.
      try {
        const data = await r.json();
        if (data && data.skipped_pipeline) {
          showStatus("Didn't catch that — tap the mic to try again.");
          return;
        }
        // === 05: ASYNC BRANCH ===
        // Long memos (>30s) come back 202 + {async:true, job_id, ...} from
        // 04's backend. Hand off to the async path; the sync M5 render
        // below only fires for short memos with briefer_text present.
        if (data && data.async && data.job_id) {
          startAsyncJob(data.job_id, data.audio_duration_s, data.queue_position || 0);
          return;
        }
        // === END 05: ASYNC BRANCH ===
        // === M5: BRIEFER ===
        // If the route returned a briefer reply, render the user's
        // transcript as a sent bubble + the briefer's response as a recv
        // bubble. Strip markdown defensively (the persona forbids it but
        // the model occasionally slips). Then consume the pending so the
        // SSE doesn't re-fire streamQuery on the same memo.
        if (data && (data.briefer_text || data.user_transcript)) {
          openChat();
          const userText = (data.user_transcript || '').trim();
          if (userText) appendBubble(makeBubble('sent', userText));
          if (data.briefer_text) {
            appendBubble(makeBubble('recv', stripMarkdown(data.briefer_text)));
          }
          if (data.pending_id) {
            __M5_BRIEFER_HANDLED.add(data.pending_id);
            fetch('/api/voice/pending/consume', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ id: data.pending_id }),
            }).catch(() => {});
          }
        }
        // === END M5: BRIEFER ===

        // === INLINE VERIFY CARD ===
        // Render the propose-approve-commit card right in the chat surface
        // when the audio_upload pipeline produced any provisional nodes /
        // edges. The card has ✓/✗ buttons that hit /api/verify/apply or
        // /api/verify/reject inline — no tab-switch.
        if (data && data.commit_summary
            && (data.commit_summary.nodes_added > 0 || data.commit_summary.edges_added > 0)) {
          openChat();
          const card = makeVerifyCardBubble(data.commit_summary, data.proposal || {});
          if (card) appendBubble(card);
        }
        // === END INLINE VERIFY CARD ===
      } catch { /* response wasn't JSON; ignore */ }
    } catch (err) {
      showStatus(`Upload error: ${err.message}`);
    }
  }

  function showStatus(msg) {
    const row = makeBubble('status', msg);
    if (row) appendBubble(row);
  }

  function wireMic() {
    micButtons().forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        handleMicTap();
      });
    });
  }
  // === END M4: TAP-TO-TOGGLE MIC ===

  // === 05: ASYNC FRONTEND ===
  // When 04's backend returns 202 + {async:true, job_id} for long memos,
  // show a sticky banner at the top of #jazz-chat-area, poll the job
  // endpoint every 2s, and replace the banner with user + briefer bubbles
  // when status flips to "done".
  //
  // Survives:
  //   • Tab backgrounding — pollJob no-ops while document.hidden, resumes
  //     on visibilitychange returning to visible.
  //   • Quick refresh — sessionStorage keeps active jobIds; init() resumes
  //     polling for any survivors. 404 on resume = drop silently.
  //   • Multiple concurrent jobs — Map<jobId, state>; banners stack at the
  //     top of the chat area (newest first), each polls independently.
  //
  // Contract with 04 backend (per docs/orchestration/2026-05-05_chat_async_emitter/04*):
  //   POST /api/inbox/audio_upload -> 202 {ok, async:true, job_id,
  //                                        audio_duration_s, queue_position}
  //   GET  /api/voice/job/<id>     -> 200 {status: "queued"|"transcribing"
  //                                                 |"done"|"error",
  //                                        queue_position?, briefer_text?,
  //                                        user_transcript?, pending_id?,
  //                                        error?}
  //                                  404 when job unknown / reaped.
  //
  // Contract with 01 (M5 briefer): on done, render user_transcript +
  // briefer_text via the same makeBubble + stripMarkdown path the sync
  // route uses.
  const ASYNC_POLL_MS = 2000;
  const ASYNC_TIMER_TICK_MS = 1000;
  const ASYNC_SESSION_KEY = 'jazz.asyncJobs';
  // jobId -> {jobId, banner, durationS, startedAt, timeoutId,
  //           tickIntervalId, pausedAt, status}
  const activeJobs = new Map();

  function asyncStorageRead() {
    try {
      if (typeof sessionStorage === 'undefined') return [];
      const raw = sessionStorage.getItem(ASYNC_SESSION_KEY);
      const arr = JSON.parse(raw || '[]');
      return Array.isArray(arr) ? arr : [];
    } catch { return []; }
  }
  function asyncStorageWrite(arr) {
    try {
      if (typeof sessionStorage === 'undefined') return;
      sessionStorage.setItem(ASYNC_SESSION_KEY, JSON.stringify(arr));
    } catch {}
  }
  function pruneSessionStorage(jobId) {
    asyncStorageWrite(asyncStorageRead().filter(j => j.jobId !== jobId));
  }
  function pushSessionStorage(jobId, durationS) {
    const arr = asyncStorageRead().filter(j => j.jobId !== jobId);
    arr.push({ jobId, durationS });
    asyncStorageWrite(arr);
  }

  function makeAsyncBanner(jobId) {
    const banner = document.createElement('div');
    banner.className = 'jazz-async-banner';
    banner.dataset.jobId = jobId;
    banner.setAttribute('aria-live', 'polite');
    const spinner = document.createElement('span');
    spinner.className = 'jazz-async-banner-spinner';
    spinner.setAttribute('aria-hidden', 'true');
    const txt = document.createElement('span');
    txt.className = 'jazz-async-banner-text';
    txt.dataset.field = 'status';
    const elapsed = document.createElement('span');
    elapsed.className = 'jazz-async-banner-elapsed';
    elapsed.dataset.field = 'elapsed';
    elapsed.textContent = '0:00';
    banner.appendChild(spinner);
    banner.appendChild(txt);
    banner.appendChild(elapsed);
    return banner;
  }

  function bannerStatusText(durationS, status, queuePos) {
    if (status === 'queued' && queuePos > 0) {
      return `Queued — ${queuePos} ahead of you`;
    }
    const dur = Math.max(1, Math.round(Number(durationS) || 0));
    return `Transcribing ${dur}s memo…`;
  }

  function formatElapsed(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function updateBanner(jobId, job, durationS) {
    const state = activeJobs.get(jobId);
    if (!state || !state.banner) return;
    const status = (job && job.status) || state.status || 'transcribing';
    state.status = status;
    const queuePos = (job && typeof job.queue_position === 'number') ? job.queue_position : 0;
    const statusEl = state.banner.querySelector('[data-field="status"]');
    if (statusEl) statusEl.textContent = bannerStatusText(durationS, status, queuePos);
  }

  function tickAsyncTimer(jobId) {
    const state = activeJobs.get(jobId);
    if (!state || !state.banner) return;
    const elapsedEl = state.banner.querySelector('[data-field="elapsed"]');
    if (elapsedEl) elapsedEl.textContent = formatElapsed(Date.now() - state.startedAt);
  }

  function startAsyncJob(jobId, durationS, queuePos) {
    if (!jobId) return;
    if (activeJobs.has(jobId)) {
      // Idempotent — second call (e.g. resume from sessionStorage) reuses
      // the existing banner + state rather than racing a duplicate poller.
      const existing = activeJobs.get(jobId);
      updateBanner(jobId, { status: existing.status || 'queued', queue_position: queuePos || 0 }, existing.durationS);
      return;
    }
    const banner = makeAsyncBanner(jobId);
    const area = document.getElementById('jazz-chat-area');
    if (area) {
      // Newest banner stacks above any prior ones (insertBefore firstChild).
      area.insertBefore(banner, area.firstChild || null);
    }
    openChat();
    const state = {
      jobId,
      banner,
      durationS: Number(durationS) || 0,
      startedAt: Date.now(),
      timeoutId: null,
      tickIntervalId: null,
      pausedAt: null,
      status: 'queued',
    };
    activeJobs.set(jobId, state);
    pushSessionStorage(jobId, state.durationS);
    updateBanner(jobId, { status: 'queued', queue_position: queuePos || 0 }, state.durationS);
    tickAsyncTimer(jobId);
    state.tickIntervalId = setInterval(() => tickAsyncTimer(jobId), ASYNC_TIMER_TICK_MS);
    state.timeoutId = setTimeout(() => pollJob(jobId), ASYNC_POLL_MS);
  }

  async function pollJob(jobId) {
    const state = activeJobs.get(jobId);
    if (!state) return;
    state.timeoutId = null;
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
      // Pause cleanly — the visibilitychange listener re-arms on visible.
      state.pausedAt = Date.now();
      return;
    }
    state.pausedAt = null;
    try {
      const r = await fetch(`/api/voice/job/${encodeURIComponent(jobId)}`);
      if (r.status === 404) {
        finalizeAsyncJob(jobId, { error: 'job_not_found' });
        return;
      }
      if (!r.ok) {
        // Transient server error — re-arm and retry.
        if (activeJobs.has(jobId)) {
          state.timeoutId = setTimeout(() => pollJob(jobId), ASYNC_POLL_MS);
        }
        return;
      }
      const job = await r.json();
      updateBanner(jobId, job, state.durationS);
      if (job && job.status === 'done') {
        finalizeAsyncJob(jobId, job);
        return;
      }
      if (job && job.status === 'error') {
        finalizeAsyncJob(jobId, { error: job.error || 'transcription_failed' });
        return;
      }
    } catch (e) {
      // Network blip / offline — keep the poll loop alive; the user is
      // waiting on this. One missed tick is fine.
    }
    if (activeJobs.has(jobId)) {
      state.timeoutId = setTimeout(() => pollJob(jobId), ASYNC_POLL_MS);
    }
  }

  function removeBanner(jobId) {
    const state = activeJobs.get(jobId);
    if (!state) return;
    if (state.timeoutId !== null) {
      clearTimeout(state.timeoutId);
      state.timeoutId = null;
    }
    if (state.tickIntervalId !== null) {
      clearInterval(state.tickIntervalId);
      state.tickIntervalId = null;
    }
    if (state.banner && state.banner.parentNode) {
      state.banner.parentNode.removeChild(state.banner);
    }
  }

  function finalizeAsyncJob(jobId, job) {
    if (!activeJobs.has(jobId)) return;
    removeBanner(jobId);
    activeJobs.delete(jobId);
    pruneSessionStorage(jobId);
    if (!job || job.error) {
      const errMsg = (job && job.error === 'job_not_found')
        ? 'Transcription job lost — please try again.'
        : 'Transcription failed. Try again.';
      const row = makeBubble('recv', errMsg);
      if (row) {
        const bubble = row.querySelector('.jazz-bubble');
        if (bubble) bubble.classList.add('jazz-bubble-error-row');
        appendBubble(row);
      }
      return;
    }
    if (job.user_transcript) {
      appendBubble(makeBubble('sent', stripMarkdown(job.user_transcript)));
    }
    if (job.briefer_text) {
      appendBubble(makeBubble('recv', stripMarkdown(job.briefer_text)));
    }
  }

  function resumePausedJobs() {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    activeJobs.forEach((state, jobId) => {
      if (state.pausedAt !== null && state.timeoutId === null) {
        state.pausedAt = null;
        // Poll immediately on resume — fresh status is what the user wants.
        state.timeoutId = setTimeout(() => pollJob(jobId), 0);
      }
    });
  }

  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('visibilitychange', resumePausedJobs);
  }

  function resumeAsyncJobsFromStorage() {
    asyncStorageRead().forEach(({ jobId, durationS }) => {
      if (jobId && !activeJobs.has(jobId)) {
        startAsyncJob(jobId, durationS || 0, 0);
      }
    });
  }

  // Expose for tests (and DOMContentLoaded resume hook).
  if (typeof window !== 'undefined') {
    window.__ASYNC = {
      activeJobs,
      startAsyncJob,
      pollJob,
      finalizeAsyncJob,
      updateBanner,
      removeBanner,
      resumePausedJobs,
      resumeAsyncJobsFromStorage,
      ASYNC_SESSION_KEY,
      ASYNC_POLL_MS,
    };
  }
  // === END 05: ASYNC FRONTEND ===

  function wireBack() {
    const back = $('#jazz-back-btn');
    if (back) back.addEventListener('click', closeChat);
  }

  // ── Voice pending stream (Agent C / Sub-10) ───────────────────────────────
  // When a voice memo arrives via the iOS Shortcut → watcher → cleaner path,
  // route it into the chat as a sent bubble + auto-query.
  let voiceES = null;
  let voiceRetryT = null;
  function startVoicePendingStream() {
    if (typeof EventSource === 'undefined') return;
    voiceES = new EventSource('/api/voice/pending');
    voiceES.addEventListener('voice', (ev) => {
      let memo; try { memo = JSON.parse(ev.data); } catch { return; }
      // === M5: BRIEFER ===
      // Skip memos that uploadAudio has already rendered briefer bubbles
      // for. This is the chat-route audio-upload path; the briefer takes
      // precedence over the SSE-driven streamQuery path. Other voice-memo
      // sources (iOS Shortcut watcher) still flow through this handler
      // because they never go through uploadAudio.
      if (memo.id && __M5_BRIEFER_HANDLED.has(memo.id)) {
        __M5_BRIEFER_HANDLED.delete(memo.id);
        return;
      }
      // === END M5: BRIEFER ===
      const text = memo.text || memo.cleaned_text || '';
      if (!text) return;
      openChat();
      appendBubble(makeBubble('sent', text));
      streamQuery(text);
      if (memo.id) {
        fetch('/api/voice/pending/consume', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: memo.id }),
        }).catch(() => {});
      }
    });
    voiceES.onerror = () => {
      try { voiceES.close(); } catch {}
      clearTimeout(voiceRetryT);
      voiceRetryT = setTimeout(startVoicePendingStream, 5000);
    };
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  function init() {
    wireSubmit();
    wireMic();
    wireBack();
    refreshDashboard();
    refreshTopConnections();
    startStatsFooter();
    startVoicePendingStream();
    resumeAsyncJobsFromStorage();   // 05: resume polling for any in-flight long-memo jobs
    setInterval(refreshDashboard, 30000);
    setInterval(refreshTopConnections, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
// === END S1: JAZZ INTEGRATION ===
