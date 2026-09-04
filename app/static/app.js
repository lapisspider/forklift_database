// Shared page behaviors: tabs, filtering dropdowns (single + multi),
// accordion (OEM view), and the search-result modal.

// ----- Tabs (Forklifts / Kits) -----
(function () {
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');
  if (!tabs.length) return;
  function activate(name) {
    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    panels.forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  }
  tabs.forEach(t => t.addEventListener('click', () => activate(t.dataset.tab)));
  if (location.hash === '#kits') activate('kits');
})();

// ----- Filtering dropdown (.combo). data-multi => chips, else single hidden value.
(function () {
  document.querySelectorAll('.combo').forEach(initCombo);

  function initCombo(combo) {
    const isMulti = combo.dataset.multi === '1';
    const input = combo.querySelector('input[type=text]');
    const list = combo.querySelector('.combo-list');
    if (!input || !list) return;
    const items = Array.from(list.querySelectorAll('.combo-item'));
    const empty = list.querySelector('.combo-empty');
    const chipsBox = combo.querySelector('.chips');
    const hiddenSingle = combo.querySelector('input[type=hidden]'); // single mode
    const selected = new Set();
    let active = -1;

    function open() { list.hidden = false; filter(); }
    function close() { list.hidden = true; active = -1; }
    function visibleItems() { return items.filter(i => !i.hidden); }

    function filter() {
      const q = input.value.trim().toLowerCase();
      let shown = 0;
      items.forEach(i => {
        const chosen = isMulti && selected.has(i.dataset.id);
        const match = !chosen && i.dataset.label.toLowerCase().includes(q);
        i.hidden = !match;
        i.classList.remove('active');
        if (match) shown++;
      });
      if (empty) empty.hidden = shown !== 0;
      active = -1;
      if (!isMulti && hiddenSingle && hiddenSingle.value) {
        const picked = items.find(i => i.dataset.serial === hiddenSingle.value);
        if (!picked || input.value !== picked.dataset.label) hiddenSingle.value = '';
      }
    }

    function highlight(idx) {
      const vis = visibleItems();
      vis.forEach(i => i.classList.remove('active'));
      if (idx >= 0 && idx < vis.length) {
        vis[idx].classList.add('active');
        vis[idx].scrollIntoView({ block: 'nearest' });
      }
    }

    // ----- multi (chips) -----
    function renderChips() {
      chipsBox.innerHTML = '';
      selected.forEach(id => {
        const item = items.find(i => i.dataset.id === id);
        const label = item ? item.dataset.label : id;
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `<span>${label}</span><button type="button" aria-label="Remove">×</button>`;
        chip.querySelector('button').addEventListener('click', () => {
          selected.delete(id);
          renderChips();
          filter();
        });
        const hid = document.createElement('input');
        hid.type = 'hidden';
        hid.name = 'forklift_ids';
        hid.value = id;
        chip.appendChild(hid);
        chipsBox.appendChild(chip);
      });
    }

    function choose(item) {
      if (isMulti) {
        selected.add(item.dataset.id);
        renderChips();
        input.value = '';
        filter();
        input.focus();
      } else {
        input.value = item.dataset.label;
        if (hiddenSingle) hiddenSingle.value = item.dataset.serial || item.dataset.id || '';
        close();
      }
    }

    if (isMulti) {
      items.filter(i => i.dataset.preselected === '1').forEach(i => selected.add(i.dataset.id));
      renderChips();
    }

    input.addEventListener('focus', open);
    input.addEventListener('input', () => { if (list.hidden) list.hidden = false; filter(); });
    input.addEventListener('keydown', (e) => {
      const vis = visibleItems();
      if (e.key === 'ArrowDown') { e.preventDefault(); if (list.hidden) open(); active = Math.min(active + 1, vis.length - 1); highlight(active); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); highlight(active); }
      else if (e.key === 'Enter') { if (!list.hidden && active >= 0 && vis[active]) { e.preventDefault(); choose(vis[active]); } }
      else if (e.key === 'Escape') { close(); }
      else if (e.key === 'Backspace' && isMulti && input.value === '' && selected.size) {
        const last = Array.from(selected).pop();
        selected.delete(last); renderChips(); filter();
      }
    });
    items.forEach(i => i.addEventListener('mousedown', (e) => { e.preventDefault(); choose(i); }));
    document.addEventListener('click', (e) => { if (!combo.contains(e.target)) close(); });
  }
})();

// ----- Accordion (OEM view): toggle the element named by data-target -----
// Open sections are remembered in localStorage so navigating into a record and
// back (a full page load) restores whatever OEM/series tabs you had open.
(function () {
  const toggles = document.querySelectorAll('.accordion-toggle');
  if (!toggles.length) return;
  const KEY = 'forklift_accordion_open';

  function load() {
    try { return new Set(JSON.parse(localStorage.getItem(KEY)) || []); }
    catch (e) { return new Set(); }
  }
  function save(set) {
    try { localStorage.setItem(KEY, JSON.stringify(Array.from(set))); }
    catch (e) { /* storage unavailable — degrade to no persistence */ }
  }

  const open = load();

  // Restore: reopen any remembered section that still exists on this page.
  toggles.forEach(btn => {
    const body = document.getElementById(btn.dataset.target);
    if (body && open.has(btn.dataset.target)) {
      body.hidden = false;
      btn.classList.add('open');
    }
  });

  toggles.forEach(btn => {
    btn.addEventListener('click', () => {
      const body = document.getElementById(btn.dataset.target);
      if (!body) return;
      body.hidden = !body.hidden;
      btn.classList.toggle('open', !body.hidden);
      if (body.hidden) open.delete(btn.dataset.target);
      else open.add(btn.dataset.target);
      save(open);
    });
  });
})();

// ----- Preserve scroll position on the database list across navigation -----
// Entering a record is a full page load, which resets scroll. Remember where
// the list was scrolled and restore it on return. Per-tab (sessionStorage) and
// keyed by the current view (path+sort+filter) so different views don't cross.
(function () {
  if (!document.querySelector('.tabs')) return; // list/index page only
  const KEY = 'forklift_scroll:' + location.pathname + location.search;
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

  let saved = null;
  try { saved = sessionStorage.getItem(KEY); } catch (e) { /* unavailable */ }
  if (saved !== null) {
    const y = parseInt(saved, 10) || 0;
    // rAF so any restored accordion heights are applied before we scroll.
    requestAnimationFrame(() => window.scrollTo(0, y));
  }

  function store() {
    try { sessionStorage.setItem(KEY, String(window.scrollY)); }
    catch (e) { /* storage unavailable — degrade to no persistence */ }
  }
  window.addEventListener('pagehide', store);
  window.addEventListener('beforeunload', store);
})();

// ----- Modal for search results -----
(function () {
  const overlay = document.getElementById('modal-overlay');
  if (!overlay) return;
  const titleEl = document.getElementById('modal-title');
  const body = document.getElementById('modal-body');
  const closeBtn = document.getElementById('modal-close');

  function openModal(title) {
    if (title && titleEl) titleEl.textContent = title;
    overlay.hidden = false;
  }
  function closeModal() {
    overlay.hidden = true;
    if (body) body.innerHTML = '';
  }

  // Open when htmx swaps content into the modal body.
  document.body.addEventListener('htmx:afterSwap', (e) => {
    if (e.detail && e.detail.target && e.detail.target.id === 'modal-body') {
      const trigger = e.detail.requestConfig && e.detail.requestConfig.elt;
      const title = trigger && trigger.dataset ? trigger.dataset.modalTitle : 'Result';
      openModal(title || 'Result');
    }
  });

  closeBtn && closeBtn.addEventListener('click', closeModal);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !overlay.hidden) closeModal(); });
})();
