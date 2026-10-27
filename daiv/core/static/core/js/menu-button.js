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
 */
document.addEventListener("alpine:init", () => {
  Alpine.data("menuButton", () => ({
    open: false,

    items() {
      return [...this.$refs.menu.querySelectorAll('[role="menuitem"], [role="menuitemcheckbox"]')];
    },

    focusAt(index) {
      const items = this.items();
      items.at(index % items.length)?.focus();
    },

    show(index) {
      this.open = true;
      this.$nextTick(() => this.focusAt(index));
    },

    toggle() {
      if (this.open) this.close();
      else this.show(0);
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
