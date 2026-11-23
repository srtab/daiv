/**
 * A button that opens a small menu of links — the WAI-ARIA menu button pattern.
 *
 * Opening moves focus to the first item (ArrowUp on the trigger: the last). Inside the
 * menu, ArrowUp/ArrowDown wrap and Home/End jump; Escape closes and returns focus to the
 * trigger, and Tab or a click outside just closes.
 *
 *     <div x-data="menuButton" @keydown.escape="onEscape($event)" @click.outside="close()">
 *       <button x-ref="trigger" @click="toggle()" @keydown.arrow-down.prevent="show(0)">
 *       <div x-ref="menu" role="menu" x-show="open" @keydown="onMenuKey($event)">
 *         <a role="menuitem" tabindex="-1" href="...">
 *
 * Escape stops propagating only while the menu is open, so a menu inside the mobile nav
 * sheet closes itself first and the sheet's own window-level Escape closes it next time.
 * Inside a `[data-menu-bounds]` box, `up` says the menu would run past the box's bottom edge
 * but fits above its component, so it can open upward; overflow below can still be scrolled to.
 */
document.addEventListener("alpine:init", () => {
  Alpine.data("menuButton", () => ({
    open: false,
    up: false,

    items() {
      return [...this.$refs.menu.querySelectorAll('[role^="menuitem"]')];
    },

    focusAt(index) {
      const items = this.items();
      items.at(index % items.length)?.focus();
    },

    show(index) {
      this.up = false;
      this.open = true;
      // x-show reveals the menu in a frame it requests while Alpine flushes `open`; requesting ours after that
      // flush measures the menu shown and flips it before the frame paints.
      queueMicrotask(() => requestAnimationFrame(() => (this.up = this.opensUp())));
      this.$nextTick(() => this.focusAt(index));
    },

    toggle() {
      if (this.open) this.close();
      else this.show(0);
    },

    opensUp() {
      const bounds = this.$root.closest("[data-menu-bounds]")?.getBoundingClientRect();
      if (!bounds) return false;
      const top = this.$root.getBoundingClientRect().top;
      const { offsetTop, offsetHeight } = this.$refs.menu;
      const reach = offsetTop - this.$root.offsetHeight + offsetHeight;
      return top + offsetTop + offsetHeight > bounds.bottom && top - reach >= bounds.top;
    },

    close(refocus = false) {
      if (!this.open) return;
      this.open = false;
      if (refocus) this.$refs.trigger.focus();
    },

    onEscape(event) {
      if (!this.open) return;
      event.stopPropagation();
      this.close(true);
    },

    onMenuKey(event) {
      const current = this.items().indexOf(document.activeElement);
      const moves = { ArrowDown: current + 1, ArrowUp: current - 1, Home: 0, End: -1 };
      if (event.key === "Tab") this.close();
      if (!(event.key in moves)) return;
      event.preventDefault();
      this.focusAt(moves[event.key]);
    },
  }));
});
