/* The settings panel's renderer.
 *
 * This file knows how to draw a CONTROL, not how to draw a TAB. The meter
 * sends a declarative spec — a flat list of nodes per page — and everything
 * here turns that into DOM. That split is deliberate: it is what keeps adding
 * a setting a Python-side change, exactly as it was when the panel was Tk, and
 * it means the layout can never drift out of step with the state that feeds it.
 *
 * Node kinds, all optional fields marked:
 *   {k:"section", t}                              a heading with a rule
 *   {k:"note",    t, warn?}                       explanatory paragraph
 *   {k:"button",  id, t, on?, tone?, p?}          the panel's workhorse
 *   {k:"field",   t, c:<control>}                 labelled row
 *   {k:"chips",   id, v, o:[{v,t}]}               a row of small toggles
 *   {k:"search",  id, v, count?, ph?}             search box + result count
 *   {k:"list",    id, h?, rows:[<row>]}           scrolling viewport
 *   {k:"gap"}                                     vertical breathing room
 * controls:
 *   {k:"select",  id, v, o:[...]}
 *   {k:"slider",  id, v, min, max, step?, live?, unit?}
 *   {k:"text",    id, v, ph?}
 *   {k:"label",   t}
 * rows:
 *   {id?, name?, cls?, meta?, icon?, t?, on?, btns?:[{id,t,p?}]}
 */
'use strict';

const $ = (sel) => document.querySelector(sel);

/* The last spec we drew, so a push that changes nothing costs nothing. */
let STATE = {};
/* key -> {el, sig} for the current page, so typing in a search box does not
   get interrupted by the list under it re-rendering. See renderPage. */
let NODES = new Map();
let READY = false;

/* ---- talking to the meter ---------------------------------------------- */
/* notify() is fire-and-forget: a toggle's real answer is the state push that
   follows it, so waiting for a return value would only add a round trip to
   something we already throw away. call() is for the few that answer. */
function notify(method, params) {
  if (!window.pywebview) return;
  window.pywebview.api.notify(method, params || {});
}
function call(method, params) {
  if (!window.pywebview) return Promise.resolve(null);
  return window.pywebview.api.call(method, params || {});
}

/* ---- window chrome ------------------------------------------------------ */
/* Both of these hand the gesture to Windows rather than tracking the mouse in
   JS: it is smoother, it snaps, and it ends by itself when the button lifts. */
/* Drag and resize are driven from here rather than handed to Windows.
 *
 * See Api.grab for the two native attempts that came first and why neither
 * survived: a topmost tool window over a fullscreen game is exactly the case
 * where Windows' own move loop will not track the mouse.
 *
 * setPointerCapture is what makes this reliable — without it the gesture dies
 * the moment the cursor leaves the 6px grip or outruns the window, which it
 * always does. Screen coordinates, not client ones, because the window is
 * moving underneath the pointer as we go. */
function wireChrome() {
  let g = null;   /* the gesture in progress */

  function begin(e, edge) {
    if (e.button !== 0) return;
    e.preventDefault();
    window.pywebview.api.grab().then((r) => {
      if (r) g = { edge, sx: e.screenX, sy: e.screenY,
                   x: r[0], y: r[1], w: r[2], h: r[3] };
    });
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function move(e) {
    if (!g) return;
    const dx = e.screenX - g.sx, dy = e.screenY - g.sy;
    if (!g.edge) {
      window.pywebview.api.place(g.x + dx, g.y + dy);
      return;
    }
    /* Each edge moves the origin, the size, or both. Written out rather than
       computed so a corner is obviously the two edges it is made of. */
    let { x, y, w, h } = g;
    if (g.edge.includes('left'))   { x = g.x + dx; w = g.w - dx; }
    if (g.edge.includes('right'))  { w = g.w + dx; }
    if (g.edge.includes('top'))    { y = g.y + dy; h = g.h - dy; }
    if (g.edge.includes('bottom')) { h = g.h + dy; }
    window.pywebview.api.place(x, y, w, h);
  }

  function end(e) {
    if (!g) return;
    g = null;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch (_) {}
    /* Tell the meter where we ended up, so it can remember it. Once here,
       rather than on every pointermove — see Api.settle. */
    window.pywebview.api.settle();
  }

  const bar = $('#titlebar');
  bar.addEventListener('pointerdown', (e) => {
    /* The header is the drag handle, but the buttons in it are buttons. A
       widget that both drags and clicks does one of them by surprise. */
    if (e.target.closest('button')) return;
    begin(e, null);
  });
  bar.addEventListener('pointermove', move);
  bar.addEventListener('pointerup', end);
  bar.addEventListener('pointercancel', end);

  document.querySelectorAll('.grip').forEach((grip) => {
    grip.addEventListener('pointerdown', (e) => begin(e, grip.dataset.edge));
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', end);
    grip.addEventListener('pointercancel', end);
  });
}

/* A brief confirmation, bottom-centre of the panel.
 *
 * Shown from the click rather than waiting for the meter to answer: opening a
 * link is silent by nature — nothing on the panel changes — and a confirmation
 * that arrived after a round trip would land well after the moment it is
 * confirming. */
let TOAST_JOB = null;
function showToast(text) {
  let t = $('#toast');
  if (!t) {
    t = el('div', null);
    t.id = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = text;
  t.classList.add('on');
  if (TOAST_JOB) clearTimeout(TOAST_JOB);
  TOAST_JOB = setTimeout(() => t.classList.remove('on'), 2200);
}

/* The user's own size preference, on top of the device scale factor the host
   already pinned to 1. A CSS zoom rather than a browser-level one so the
   slider can move without recreating the WebView2 environment. */
function setZoom(pct) {
  const z = Math.max(50, Math.min(200, Number(pct) || 100));
  document.documentElement.style.zoom = (z / 100).toString();
}

/* ---- element helpers ---------------------------------------------------- */
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

/* Inline **bold** and `code`, as ELEMENTS.
 *
 * Never innerHTML. The help text is ours, but this same path renders a player's
 * name and a shard id, and building markup from a string is how one of those
 * turns into script one day. Splitting on the markers and appending text nodes
 * cannot do that whatever the input says. */
function inline(parent, text) {
  const parts = String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  parts.forEach((p) => {
    if (!p) return;
    if (p.startsWith('**') && p.endsWith('**') && p.length > 4) {
      parent.appendChild(el('strong', null, p.slice(2, -2)));
    } else if (p.startsWith('`') && p.endsWith('`') && p.length > 2) {
      parent.appendChild(el('code', null, p.slice(1, -1)));
    } else {
      parent.appendChild(document.createTextNode(p));
    }
  });
  return parent;
}

/* Which class a button wears. `on` is a standing setting that is currently
   true; `tone` carries the exceptions (a destructive action, a disabled one). */
function btnClass(node) {
  let c = 'btn';
  if (node.on) c += ' on';
  if (node.tone) c += ' ' + node.tone;
  return c;
}

/* ---- controls ----------------------------------------------------------- */
function buildControl(c) {
  if (c.k === 'select') {
    const s = el('select');
    (c.o || []).forEach((o) => {
      const opt = el('option', null, typeof o === 'string' ? o : o.t);
      opt.value = typeof o === 'string' ? o : o.v;
      s.appendChild(opt);
    });
    s.value = c.v;
    s.addEventListener('change', () => notify(c.id, { value: s.value }));
    return s;
  }
  if (c.k === 'slider') {
    const wrap = el('div', 'ctl');
    const r = el('input');
    r.type = 'range';
    r.min = c.min; r.max = c.max; r.step = c.step || 1; r.value = c.v;
    const out = el('span', 'slider-val', c.v + (c.unit || ''));
    /* Live sliders report every step (volume — hearing the level while you
       drag is the point). The rest report on release, because each step
       repaints whole windows and doing that per pixel of drag is visibly
       slow. Either way the readout follows the handle immediately. */
    r.addEventListener('input', () => {
      out.textContent = r.value + (c.unit || '');
      if (c.live) notify(c.id, { value: Number(r.value) });
    });
    r.addEventListener('change', () => {
      if (!c.live) notify(c.id, { value: Number(r.value) });
    });
    wrap.appendChild(r);
    wrap.appendChild(out);
    return wrap;
  }
  if (c.k === 'text') {
    const i = el('input');
    i.type = 'text';
    i.value = c.v || '';
    if (c.ph) i.placeholder = c.ph;
    wireTyping(i);
    i.addEventListener('input', () => notify(c.id, { value: i.value }));
    return i;
  }
  return el('span', 'meta', c.t || '');
}

/* A focused text box holds the keyboard, and while it does the GAME cannot see
   its own Escape — so the first press has to leave the box and the second
   reaches the game and closes its menu. The meter is told either way, because
   it has its own focus handshake to run on the far side. */
function wireTyping(input) {
  input.addEventListener('focus', () => window.pywebview.api.typing(true));
  input.addEventListener('blur', () => window.pywebview.api.typing(false));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' || e.key === 'Enter') {
      input.blur();
      e.preventDefault();
      e.stopPropagation();
    }
  });
}

/* ---- nodes -------------------------------------------------------------- */
function buildNode(n) {
  switch (n.k) {
    case 'section':
      return el('div', 'section', n.t);

    case 'note':
      return el('p', 'note' + (n.warn ? ' warn' : ''), n.t);

    /* The three help-article kinds. Body text rather than the small dim italic
       a `note` is: an article is meant to be read, not glanced at. */
    case 'prose':
      return inline(el('p', 'prose'), n.t);

    case 'bullets': {
      const ul = el('ul', 'bullets');
      (n.items || []).forEach((it) => ul.appendChild(inline(el('li'), it)));
      return ul;
    }

    case 'code':
      return el('pre', 'code', n.t);

    /* The fundraiser: a tinted card with the logo centred on it and the blurb
       beneath. One node so the whole thing reads as a single object — and so
       the button is the LOGO rather than a bare link under some text.
       The image is optional: with no logo file it falls back to a wordmark,
       so the card works rather than showing a broken picture. */
    case 'support': {
      const card = el('div', 'support');
      const b = el('button', 'linkcard');
      if (n.img) {
        const im = el('img');
        im.src = n.img;
        im.alt = n.t || '';
        b.appendChild(im);
      } else {
        b.appendChild(el('span', 'wordmark', n.t || 'Support'));
      }
      b.title = n.url || '';
      b.addEventListener('click', () => {
        notify(n.id, {});
        if (n.toast) showToast(n.toast);
      });
      card.appendChild(b);
      (n.paras || []).forEach((p) => card.appendChild(inline(el('p'), p)));
      return card;
    }

    case 'gap':
      return el('div', 'gap');

    case 'button': {
      const b = el('button', btnClass(n), n.t);
      if (n.tone !== 'disabled') {
        b.addEventListener('click', () => notify(n.id, n.p || {}));
      }
      return b;
    }

    case 'field': {
      const row = el('div', 'field');
      row.appendChild(el('label', null, n.t));
      const c = buildControl(n.c);
      if (!c.classList.contains('ctl')) {
        const w = el('div', 'ctl');
        w.appendChild(c);
        row.appendChild(w);
      } else {
        row.appendChild(c);
      }
      return row;
    }

    case 'chips': {
      const row = el('div', 'chiprow');
      (n.o || []).forEach((o) => {
        const b = el('button', 'chip' + (o.v === n.v ? ' active' : ''), o.t);
        b.addEventListener('click', () => notify(n.id, { value: o.v }));
        row.appendChild(b);
      });
      (n.extra || []).forEach((x) => {
        const b = el('button', btnClass(x), x.t);
        b.style.width = 'auto';
        b.style.margin = '0 0 0 auto';
        b.addEventListener('click', () => notify(x.id, x.p || {}));
        row.appendChild(b);
      });
      return row;
    }

    case 'search': {
      const row = el('div', 'searchrow');
      row.appendChild(el('label', null, 'Search'));
      const i = el('input');
      i.type = 'text';
      i.value = n.v || '';
      if (n.ph) i.placeholder = n.ph;
      wireTyping(i);
      i.addEventListener('input', () => notify(n.id, { value: i.value }));
      row.appendChild(i);
      row.appendChild(el('span', 'count', n.count || ''));
      return row;
    }

    case 'list': {
      const box = el('div', 'list' + (n.grow ? ' grow' : ''));
      if (n.h && !n.grow) box.style.maxHeight = n.h + 'px';
      if (!n.rows || !n.rows.length) {
        box.appendChild(el('div', 'empty', n.empty || 'Nothing here yet.'));
        return box;
      }
      n.rows.forEach((r) => box.appendChild(buildRow(r)));
      return box;
    }

    default:
      return el('div');
  }
}

/* The status icon sprite sheet: one image for all 242 icons, handed over once
   per page load. Rows carry a CELL NUMBER, and the position is worked out
   here — so a list of icons costs an integer per row instead of an image. */
let SHEET = null;
window.setIconSheet = function (uri, cell, cols) {
  SHEET = { uri, cell: cell || 64, cols: cols || 16 };
  document.documentElement.style.setProperty('--icon-sheet', `url(${uri})`);
  /* Rows already on screen were built before the sheet arrived. */
  NODES = new Map();
  if (STATE.page) { $('#page').textContent = ''; renderPage(STATE.page); }
};

/* Point one 24px cell at the right part of the sheet. The sheet is scaled as a
   whole (background-size) so each 64px cell lands on a 24px box. */
function paintIcon(node, cellIndex) {
  if (!SHEET || cellIndex === null || cellIndex === undefined) return;
  const SIZE = 24;
  const col = cellIndex % SHEET.cols;
  const rowN = Math.floor(cellIndex / SHEET.cols);
  node.style.backgroundSize = `${SHEET.cols * SIZE}px auto`;
  node.style.backgroundPosition = `-${col * SIZE}px -${rowN * SIZE}px`;
}

function buildRow(r) {
  const row = el('div', 'row' + (r.on ? ' on' : ''));
  if (r.icon !== undefined && r.icon !== null) {
    const ic = el('div', 'icon');
    paintIcon(ic, r.icon);
    row.appendChild(ic);
  }
  if (r.name !== undefined) row.appendChild(el('span', 'name', r.name));
  if (r.cls !== undefined) row.appendChild(el('span', 'cls', r.cls));
  if (r.t !== undefined) row.appendChild(el('span', 'name', r.t));
  if (r.meta !== undefined) row.appendChild(el('span', 'meta', r.meta));
  (r.btns || []).forEach((b) => {
    const btn = el('button', 'rowbtn', b.t);
    btn.addEventListener('click', () => notify(b.id, b.p || {}));
    row.appendChild(btn);
  });
  return row;
}

/* ---- the page ----------------------------------------------------------- */
/* Keyed reconciliation rather than "empty the container and rebuild".
 *
 * Rebuilding is cheap in DOM terms, but it also drops the caret out of a
 * search box and resets a list's scroll — and the panel pushes state on every
 * keystroke, because that is what filters the list. So each node is matched by
 * key, and only the ones whose content actually changed are replaced.
 */
function renderPage(nodes) {
  const page = $('#page');
  const next = new Map();
  let prevEl = null;

  nodes.forEach((n, i) => {
    const key = n.k + ':' + (n.id || i);
    const sig = JSON.stringify(n);
    const had = NODES.get(key);
    let node;
    if (had && had.sig === sig) {
      node = had.el;                      /* untouched — leave it alone */
    } else {
      node = buildNode(n);
      if (had) {
        page.replaceChild(node, had.el);
      }
    }
    next.set(key, { el: node, sig });
    /* Put it in the right place, without disturbing anything already there. */
    const want = prevEl ? prevEl.nextSibling : page.firstChild;
    if (node !== want) page.insertBefore(node, want);
    prevEl = node;
  });

  /* Anything left from the previous page is gone. */
  NODES.forEach((v, k) => { if (!next.has(k)) v.el.remove(); });
  NODES = next;
}

function renderTabs(tabs, active) {
  const nav = $('#nav');
  const sig = JSON.stringify([tabs, active]);
  if (nav.dataset.sig === sig) return;
  nav.dataset.sig = sig;
  nav.textContent = '';
  tabs.forEach((t) => {
    const b = el('button', t === active ? 'active' : '', t);
    b.addEventListener('click', () => notify('set_tab', { value: t }));
    nav.appendChild(b);
  });
}

/* ---- the state push ----------------------------------------------------- */
/* Called by the host with a JSON *string* — the state carries player names and
   shard ids straight off the wire, and interpolating those into a script
   expression would break the panel the first time somebody had a quote in
   their name. */
window.applyState = function (json) {
  let s;
  try {
    s = JSON.parse(json);
  } catch (e) {
    return;
  }
  const prev = STATE;
  STATE = s;

  if (s.zoom !== prev.zoom) setZoom(s.zoom);
  if (s.version !== prev.version) $('#version').textContent = 'v' + s.version;
  if (s.shard !== prev.shard) $('#shard').textContent = s.shard || '…';
  if (s.quit !== prev.quit || s.quitArmed !== prev.quitArmed) {
    const q = $('#quit');
    q.textContent = s.quit || 'Quit';
    /* Armed reads as a warning rather than as a link — the second click is the
       one that ends the session. */
    q.className = 'btn warn' + (s.quitArmed ? ' armed' : '');
  }

  const b = $('#banner');
  if (JSON.stringify(s.banner) !== JSON.stringify(prev.banner)) {
    b.textContent = (s.banner && s.banner.t) || '';
    b.className = s.banner && s.banner.update ? 'update' : '';
  }

  /* The active tab changing is what makes the page a different page, so the
     reconciler is told to start clean rather than matching one tab's nodes
     against another's by position. */
  if (s.tab !== prev.tab) {
    $('#page').textContent = '';
    NODES = new Map();
    $('#page').scrollTop = 0;
  }
  renderTabs(s.tabs || [], s.tab);
  renderPage(s.page || []);

  /* Tells the meter what actually landed on screen. Cheap, and it is the
     difference between "the push was written to the pipe" and "the panel drew
     it" — the two failure modes either side of the bridge look identical from
     the Python end otherwise. */
  notify('rendered', { tab: s.tab, nodes: NODES.size,
                       h: document.body.scrollHeight });
};

/* ---- boot --------------------------------------------------------------- */
function boot() {
  if (READY) return;
  READY = true;
  wireChrome();
  document.querySelectorAll('[data-act]').forEach((b) => {
    b.addEventListener('click', () => notify(b.dataset.act, {}));
  });
  $('#banner').addEventListener('click', () => {
    if ($('#banner').classList.contains('update')) notify('banner_clicked', {});
  });
  /* Escape anywhere else on the panel means "give the game its key back".
     Without this the panel would swallow it and the game's own menu would
     refuse to close while the panel had focus. */
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') notify('escape', {});
  });
  notify('boot', {});
}

window.addEventListener('pywebviewready', boot);
/* pywebviewready can fire before this script runs if the bridge was quick. */
if (window.pywebview) boot();
