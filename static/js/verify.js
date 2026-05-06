// === M2: VERIFY MOBILE — JS ===
// Reads checkboxes across all open + closed event cards. Submits batched
// apply / reject calls against the existing /api/verify/* endpoints.
// Apply is a single call (server doesn't filter by source). Reject is per-
// source (server cascade-deletes scoped to one provisional_source per call).
(() => {
  const root = document.querySelector('[data-verify-jazz="root"]');
  if (!root) return;

  const applyBtn  = document.getElementById('jazz-verify-apply-btn');
  const rejectBtn = document.getElementById('jazz-verify-reject-btn');

  // ── Collect checked items, grouped by provisional_source ─────────────────
  function collectCheckedBySource() {
    const groups = new Map();
    root.querySelectorAll('input[type="checkbox"][data-event]').forEach(inp => {
      if (!inp.checked) return;
      const src = inp.dataset.event;
      const kind = inp.dataset.kind;
      const key = inp.dataset.key;
      if (!groups.has(src)) groups.set(src, { node_ids: [], edge_keys: [] });
      const g = groups.get(src);
      if (kind === 'node') g.node_ids.push(key);
      else if (kind === 'edge') g.edge_keys.push(key);
    });
    return groups;
  }

  function totalChecked(groups) {
    let n = 0;
    groups.forEach(g => { n += g.node_ids.length + g.edge_keys.length; });
    return n;
  }

  function eventCard(src) {
    return root.querySelector(`.jazz-verify-event[data-event="${CSS.escape(src)}"]`);
  }

  function setEventError(src, message) {
    const card = eventCard(src);
    if (!card) return;
    const slot = card.querySelector('[data-role="error"]');
    if (!slot) return;
    slot.textContent = message || '';
    slot.hidden = !message;
    if (message) card.open = true;
  }

  function removeCardOnSuccess(src) {
    const card = eventCard(src);
    if (card) card.remove();
  }

  function maybeShowAllClear() {
    const remaining = root.querySelectorAll('.jazz-verify-event').length;
    const remainingBatches = root.querySelectorAll('.jazz-verify-batch').length;
    if (remaining === 0 && remainingBatches === 0) {
      const actions = document.querySelector('[data-verify-jazz="actions"]');
      if (actions) actions.remove();
      const empty = document.createElement('div');
      empty.className = 'jazz-verify-empty';
      empty.dataset.verifyJazz = 'empty';
      empty.innerHTML =
        '<div class="jazz-verify-empty-glyph" aria-hidden="true">✓</div>' +
        '<p>All clear. Nothing to verify.</p>';
      root.appendChild(empty);
      const count = root.querySelector('.jazz-verify-count');
      if (count) count.textContent = 'all clear';
    }
  }

  // ── Apply (verify) — single batched call ────────────────────────────────
  async function doApply() {
    const groups = collectCheckedBySource();
    if (totalChecked(groups) === 0) return;

    applyBtn.disabled  = true;
    rejectBtn.disabled = true;
    const originalLabel = applyBtn.textContent;
    applyBtn.textContent = 'Verifying…';

    const allNodes = [];
    const allEdges = [];
    groups.forEach(g => {
      allNodes.push(...g.node_ids);
      allEdges.push(...g.edge_keys);
    });

    try {
      const r = await fetch('/api/verify/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ node_ids: allNodes, edge_keys: allEdges }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.ok) {
        groups.forEach((_g, src) => removeCardOnSuccess(src));
        maybeShowAllClear();
      } else {
        const msg = data.error || `HTTP ${r.status}`;
        groups.forEach((_g, src) => setEventError(src, 'Verify failed: ' + msg));
      }
    } catch (err) {
      groups.forEach((_g, src) => setEventError(src, 'Network error: ' + err.message));
    } finally {
      applyBtn.disabled  = false;
      rejectBtn.disabled = false;
      applyBtn.textContent = originalLabel;
    }
  }

  // ── Reject — one call per provisional_source (server-scoped delete) ─────
  async function doReject() {
    const groups = collectCheckedBySource();
    if (totalChecked(groups) === 0) return;
    const sources = Array.from(groups.keys());
    if (!confirm(`Reject ${totalChecked(groups)} item(s) across ${sources.length} source(s)? Hard delete.`)) {
      return;
    }

    applyBtn.disabled  = true;
    rejectBtn.disabled = true;
    const originalLabel = rejectBtn.textContent;
    rejectBtn.textContent = 'Rejecting…';

    const failures = [];
    for (const [src, g] of groups.entries()) {
      try {
        const r = await fetch('/api/verify/reject', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            provisional_source: src,
            node_ids: g.node_ids,
            edge_keys: g.edge_keys,
          }),
        });
        const data = await r.json().catch(() => ({}));
        if (r.ok && data.ok) {
          removeCardOnSuccess(src);
          setEventError(src, '');
        } else {
          failures.push(src);
          setEventError(src, 'Reject failed: ' + (data.error || `HTTP ${r.status}`));
        }
      } catch (err) {
        failures.push(src);
        setEventError(src, 'Network error: ' + err.message);
      }
    }

    if (failures.length === 0) maybeShowAllClear();
    applyBtn.disabled  = false;
    rejectBtn.disabled = false;
    rejectBtn.textContent = originalLabel;
  }

  // ── Bulk batch buttons (per-batch apply / reject by prefix) ─────────────
  async function doBatch(action, prefix, btn) {
    const verb = action === 'reject' ? 'REJECT (hard-delete)' : 'verify';
    if (!confirm(`Bulk ${verb} all items in batch "${prefix}"?`)) return;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    const endpoint = action === 'reject' ? '/api/verify/reject_batch' : '/api/verify/apply_batch';
    try {
      const r = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provisional_source_prefix: prefix }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.ok) {
        const card = root.querySelector(
          `.jazz-verify-batch[data-batch-prefix="${CSS.escape(prefix)}"]`
        );
        if (card) card.remove();
        maybeShowAllClear();
      } else {
        alert('Batch action failed: ' + (data.error || `HTTP ${r.status}`));
        btn.disabled = false;
        btn.textContent = original;
      }
    } catch (err) {
      alert('Network error: ' + err.message);
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  if (applyBtn)  applyBtn.addEventListener('click', doApply);
  if (rejectBtn) rejectBtn.addEventListener('click', doReject);
  document.querySelectorAll('button[data-batch-action]').forEach(btn => {
    btn.addEventListener('click', () => doBatch(btn.dataset.batchAction, btn.dataset.batchPrefix, btn));
  });

  // === 03: VERIFY FOCUS ===
  // /verify?focus=<node_id> deep-link from M3 inline graph-add cards. Find the
  // event card whose data-node-ids contains <node_id>, expand it, scroll into
  // view, flash the border. Silent no-op if id isn't on the page (already
  // verified or rejected — the user shouldn't see an error).
  function applyFocusFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const focusId = params.get('focus');
    if (!focusId) return;
    const cards = root.querySelectorAll('.jazz-verify-event[data-node-ids]');
    let target = null;
    for (const c of cards) {
      const ids = (c.dataset.nodeIds || '').split(',').map(s => s.trim()).filter(Boolean);
      if (ids.includes(focusId)) { target = c; break; }
    }
    if (!target) return;
    target.open = true;
    setTimeout(() => {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.add('jazz-verify-event-flash');
      setTimeout(() => target.classList.remove('jazz-verify-event-flash'), 1500);
    }, 100);
  }
  applyFocusFromUrl();
  // === END 03: VERIFY FOCUS ===
})();
