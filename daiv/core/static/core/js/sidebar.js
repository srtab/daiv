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
 * `[` toggles it, except while typing, with a modifier held, or while a dialog, sheet or
 * menu is showing: the key belongs to whatever has focus then.
 */
document.addEventListener("alpine:init", () => {
  const STORAGE_KEY = "daiv.sidebar";
  const WIDE = "(min-width: 64rem)";
  const TYPING = 'input, textarea, select, [contenteditable]:not([contenteditable="false"])';
  // The floating-surface roster in `input.css`, plus anything announcing itself as a dialog or menu.
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

  function surfaceOpen() {
    // A static one is a column, not a surface: the session details panel from 1100px.
    return [...document.querySelectorAll(SURFACES)].some(
      (el) => el.checkVisibility() && getComputedStyle(el).position !== "static",
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
      if (event.key !== "[" || event.defaultPrevented) return;
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      if (!window.matchMedia(WIDE).matches) return;
      const target = event.target;
      if (target?.isContentEditable || target?.closest?.(TYPING)) return;
      if (surfaceOpen()) return;
      event.preventDefault();
      this.toggle();
    },
  });
});
