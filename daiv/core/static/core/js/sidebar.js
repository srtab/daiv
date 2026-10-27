/**
 * The full sidebar's collapse to the icon rail, from `lg:` up — `data-sidebar="rail"` on
 * `<html>`, which `input.css` reads to apply the rail tier and narrow `--app-sidebar-width`.
 *
 * base.html's head script sets the attribute from `localStorage` before first paint, so a
 * collapsed sidebar never flashes open; this store takes over from that attribute.
 *
 *     <button @click="$store.sidebar.toggle()" :aria-expanded="(!$store.sidebar.rail).toString()">
 *     <div @keydown.window="$store.sidebar.onKey($event)">
 *
 * `[` toggles it while its button is on screen, except while typing, with Ctrl or Meta held
 * (AltGr and Option still type `[` on many layouts), or while a dialog, sheet or menu is showing.
 */
document.addEventListener("alpine:init", () => {
  const STORAGE_KEY = "daiv.sidebar";
  const TYPING = 'input, textarea, select, [contenteditable]:not([contenteditable="false"])';
  // The floating-surface roster in `input.css` (test_sidebar_js.py keeps them in step), plus dialogs and menus.
  const SURFACES = [
    "dialog[open]",
    '[role="dialog"]',
    '[role="menu"]',
    '[aria-modal="true"]',
    ".surface-rise",
    ".picker-popover",
    ".composer-sheet",
    ".composer-autocomplete",
    ".filter-menu",
    ".card__menu-panel",
    ".new-split__menu",
    ".sidebar__account-menu",
    ".session-menu",
    ".session-details",
  ].join(", ");

  function shown(el) {
    return el.checkVisibility?.() ?? el.getClientRects().length > 0;
  }

  function surfaceOpen() {
    // A static one is a column, not a surface: the session details panel from 1100px.
    return [...document.querySelectorAll(SURFACES)].some(
      (el) => shown(el) && getComputedStyle(el).position !== "static",
    );
  }

  Alpine.store("sidebar", {
    rail: document.documentElement.dataset.sidebar === "rail",

    toggle() {
      this.rail = !this.rail;
      if (this.rail) document.documentElement.dataset.sidebar = "rail";
      else delete document.documentElement.dataset.sidebar;
      try {
        localStorage.setItem(STORAGE_KEY, this.rail ? "rail" : "full");
      } catch {
        // Blocked storage only means the next page load starts expanded.
      }
    },

    onKey(event) {
      if (event.key !== "[" || event.repeat || event.defaultPrevented) return;
      if ((event.ctrlKey || event.metaKey) && !event.getModifierState?.("AltGraph")) return;
      const button = document.querySelector(".sidebar__toggle");
      if (!button || !shown(button)) return;
      const target = event.target;
      if (target?.isContentEditable || target?.closest?.(TYPING)) return;
      if (surfaceOpen()) return;
      event.preventDefault();
      this.toggle();
    },
  });
});
