// Dashboard icons (Lucide, vendored as one sprite: /static/icons/sprite.svg).
//
// window.EdgeIcon.svg(name, { size: 'sm' | 'md' | 'lg', label, cls }) returns markup:
//   <svg class="icon icon-md" aria-hidden="true" focusable="false"><use href="/static/icons/sprite.svg#i-NAME"></use></svg>
// With a label the icon is announced instead: role="img" aria-label="...".
// window.EdgeIcon.el(name, opts) returns the same as an SVGElement.
// An unknown name renders the neutral circle and warns once in the console; it never throws.
//
// Loaded before every other dashboard script (index.html, studio.html). The
// name list below is rewritten by scripts/build_icon_sprite.py together with
// the sprite: add an icon there, never by hand in only one of the two places.
(function () {
  'use strict';

  var SPRITE = '/static/icons/sprite.svg';
  var FALLBACK = 'circle';
  var SIZES = { sm: 'icon-sm', md: 'icon-md', lg: 'icon-lg' };
  var NS = 'http://www.w3.org/2000/svg';

  var NAMES = [
    /* NAMES:BEGIN */
    'activity',
    'arrow-left',
    'arrow-right',
    'arrow-up',
    'arrow-up-down',
    'ban',
    'bell',
    'camera',
    'cctv',
    'chart-column',
    'check',
    'chevron-down',
    'chevron-left',
    'chevron-right',
    'chevron-up',
    'circle',
    'circle-alert',
    'circle-check',
    'circle-help',
    'circle-x',
    'clock',
    'copy',
    'door-open',
    'download',
    'external-link',
    'eye',
    'eye-off',
    'film',
    'filter',
    'footprints',
    'gem',
    'house',
    'image',
    'info',
    'layout-dashboard',
    'lock',
    'log-in',
    'log-out',
    'map',
    'maximize-2',
    'menu',
    'minimize-2',
    'minus',
    'monitor',
    'moon',
    'more-horizontal',
    'move-horizontal',
    'package',
    'pause',
    'pencil',
    'pin',
    'play',
    'plus',
    'power',
    'printer',
    'receipt',
    'refresh-cw',
    'save',
    'scan',
    'search',
    'settings',
    'shield-alert',
    'shield-check',
    'shopping-cart',
    'siren',
    'smartphone',
    'store',
    'sun',
    'sun-moon',
    'trash-2',
    'trending-up',
    'triangle-alert',
    'undo-2',
    'upload',
    'user',
    'users',
    'video',
    'x',
    'zoom-in',
    'zoom-out'
    /* NAMES:END */
  ];

  var known = {};
  for (var i = 0; i < NAMES.length; i++) known[NAMES[i]] = true;
  var warned = {};

  function resolve(name) {
    var n = String(name == null ? '' : name).replace(/^i-/, '');
    if (known[n]) return n;
    if (!warned[n]) {
      warned[n] = true;
      try { console.warn('EdgeIcon: no icon named "' + n + '"; showing a circle instead.'); } catch (_) { /* no console */ }
    }
    return FALLBACK;
  }

  function escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function parts(name, opts) {
    var o = opts || {};
    var size = SIZES[o.size] || SIZES.md;
    var cls = 'icon ' + size + (o.cls ? ' ' + String(o.cls) : '');
    var label = o.label == null ? '' : String(o.label).trim();
    return { id: resolve(name), cls: cls, label: label };
  }

  function href(id) {
    return SPRITE + '#i-' + id;
  }

  function svg(name, opts) {
    var p = parts(name, opts);
    var a11y = p.label ? 'role="img" aria-label="' + escapeAttr(p.label) + '"' : 'aria-hidden="true"';
    return '<svg class="' + escapeAttr(p.cls) + '" ' + a11y + ' focusable="false"><use href="' + href(p.id) + '"></use></svg>';
  }

  function el(name, opts) {
    var p = parts(name, opts);
    var node = document.createElementNS(NS, 'svg');
    node.setAttribute('class', p.cls);
    if (p.label) {
      node.setAttribute('role', 'img');
      node.setAttribute('aria-label', p.label);
    } else {
      node.setAttribute('aria-hidden', 'true');
    }
    node.setAttribute('focusable', 'false');
    var use = document.createElementNS(NS, 'use');
    use.setAttribute('href', href(p.id));
    node.appendChild(use);
    return node;
  }

  function has(name) {
    return !!known[String(name == null ? '' : name).replace(/^i-/, '')];
  }

  window.EdgeIcon = {
    svg: svg,
    el: el,
    has: has,
    names: function () { return NAMES.slice(); },
    sprite: SPRITE,
  };
})();
