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
  // `.sheet-backdrop` catches the session details panel: a sheet below 1100px, a column above, so no `data-surface`.
  const SURFACES = '[data-surface], dialog[open], [role="dialog"], [role="menu"], [aria-modal="true"], .sheet-backdrop';

  function shown(el) {
    return el.checkVisibility?.() ?? el.getClientRects().length > 0;
  }

  function surfaceOpen() {
    return [...document.querySelectorAll(SURFACES)].some(shown);
  }

  Alpine.store("sidebar", {
    rail: document.documentElement.dataset.sidebar === "rail",

    toggle() {
      this.rail = !this.rail;
      if (this.rail) document.documentElement.dataset.sidebar = "rail";
      else delete document.documentElement.dataset.sidebar;
      try {
        if (this.rail) localStorage.setItem(STORAGE_KEY, "rail");
        else localStorage.removeItem(STORAGE_KEY);
      } catch {
        // Blocked storage only means the next page load starts expanded.
      }
    },

    onKey(event) {
      if (event.key !== "[" || event.repeat || event.defaultPrevented) return;
      if ((event.ctrlKey || event.metaKey) && !event.getModifierState?.("AltGraph")) return;
      const target = event.target;
      if (target?.isContentEditable || target?.closest?.(TYPING)) return;
      const button = document.querySelector(".sidebar__toggle");
      if (!button || !shown(button)) return;
      if (surfaceOpen()) return;
      event.preventDefault();
      this.toggle();
    },
  });
});
