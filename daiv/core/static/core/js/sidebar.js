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
 *
 * `/` opens the session search under the same rules: it focuses the sessions page's search box
 * when that page is open, and otherwise follows the Sessions search icon, if the sidebar has one.
 *
 * The full sidebar's width follows its right edge, `.sidebar__resize`: drag it, nudge it with the
 * arrow keys, or double-click it to reset. The store writes `--app-sidebar-chosen` on `<html>` and
 * `input.css` clamps it between `--app-sidebar-min` and `--app-sidebar-max`, so `width` is the
 * width measured after that clamp, and that is what `localStorage` keeps (`daiv.sidebar.width`).
 * base.html's head script restores it before first paint, like the rail.
 *
 *     <div role="separator" @pointerdown="$store.sidebar.resizeStart($event)"
 *          @keydown="$store.sidebar.resizeKey($event)" @dblclick="$store.sidebar.resetWidth()">
 *
 * `sidebarMenus` is one sidebar copy's menu: the cog slides Settings in, Back and Escape slide it
 * out, and focus follows to Back or to the cog. A settings page starts on Settings; nothing is
 * remembered across pages.
 *
 *     <aside x-data="sidebarMenus(false)">  <button x-ref="cog" @click="showSettings(!settings)">
 */
document.addEventListener("alpine:init", () => {
  const STORAGE_KEY = "daiv.sidebar";
  const WIDTH_KEY = "daiv.sidebar.width";
  const WIDTH_VAR = "--app-sidebar-chosen";
  const STEP = 16;
  const TYPING = 'input, textarea, select, [contenteditable]:not([contenteditable="false"])';
  // `.sheet-backdrop` catches the session details panel: a sheet below 1100px, a column above, so no `data-surface`.
  const SURFACES = '[data-surface], dialog[open], [role="dialog"], [role="menu"], [aria-modal="true"], .sheet-backdrop';

  function shown(el) {
    return el.checkVisibility?.() ?? el.getClientRects().length > 0;
  }

  function surfaceOpen() {
    return [...document.querySelectorAll(SURFACES)].some(shown);
  }

  function bound(name) {
    return parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
  }

  Alpine.data("sidebarMenus", (settings) => ({
    settings,

    showSettings(open) {
      this.settings = open;
      // The menu being shown is still inert until Alpine's effects run.
      this.$nextTick(() => (open ? this.$refs.back : this.$refs.cog).focus({ preventScroll: true }));
    },
  }));

  Alpine.store("sidebar", {
    rail: document.documentElement.dataset.sidebar === "rail",
    width: null,
    minWidth: null,
    maxWidth: null,

    measure() {
      const aside = document.getElementById("app-sidebar");
      if (!aside) return;
      this.width = Math.round(aside.getBoundingClientRect().width);
      this.minWidth = Math.round(bound("--app-sidebar-min"));
      this.maxWidth = Math.round(bound("--app-sidebar-max"));
    },

    resizeStart(event) {
      if (event.button !== 0) return;
      event.preventDefault();
      const handle = event.currentTarget;
      const root = document.documentElement;
      handle.setPointerCapture(event.pointerId);
      this.measure();
      const startX = event.clientX;
      const startWidth = this.width;
      root.dataset.sidebarResizing = "";
      const move = (e) => this._apply(startWidth + e.clientX - startX);
      const end = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", end);
        handle.removeEventListener("pointercancel", end);
        delete root.dataset.sidebarResizing;
        this._remember();
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", end);
      handle.addEventListener("pointercancel", end);
    },

    resizeKey(event) {
      this.measure();
      const targets = {
        ArrowLeft: this.width - STEP,
        ArrowRight: this.width + STEP,
        Home: this.minWidth,
        End: this.maxWidth,
      };
      if (!(event.key in targets)) return;
      event.preventDefault();
      this._apply(targets[event.key]);
      this._remember();
    },

    resetWidth() {
      this._apply(0);
      this._remember();
    },

    _apply(px) {
      document.documentElement.style.setProperty(WIDTH_VAR, `${Math.round(px)}px`);
      this.measure();
    },

    _remember() {
      const atDefault = this.width <= this.minWidth;
      if (atDefault) document.documentElement.style.removeProperty(WIDTH_VAR);
      else document.documentElement.style.setProperty(WIDTH_VAR, `${this.width}px`);
      try {
        if (atDefault) localStorage.removeItem(WIDTH_KEY);
        else localStorage.setItem(WIDTH_KEY, String(this.width));
      } catch {
        // Blocked storage only means the next page load starts at the default width.
      }
    },

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
      if ((event.key !== "[" && event.key !== "/") || event.repeat || event.defaultPrevented) return;
      if ((event.ctrlKey || event.metaKey) && !event.getModifierState?.("AltGraph")) return;
      const target = event.target;
      if (target?.isContentEditable || target?.closest?.(TYPING)) return;
      if (event.key === "/") {
        this._search(event);
        return;
      }
      const button = document.querySelector(".sidebar__toggle");
      if (!button || !shown(button)) return;
      if (surfaceOpen()) return;
      event.preventDefault();
      this.toggle();
    },

    _search(event) {
      const field = document.getElementById("search");
      const box = field && shown(field) ? field : null;
      const link = document.querySelector('[data-testid="nav-sessions-search"]');
      if ((!box && !link) || surfaceOpen()) return;
      event.preventDefault();
      if (box) box.focus();
      else window.location.assign(link.href);
    },
  });
});
