/**
 * The sidebar's session and schedule lists: which schedules are expanded, how far "Show more"
 * reached, and swapping in fresh rows when runs start or finish.
 *
 * The rows are server-rendered (`dashboard/_sidebar_lists.html`) into every
 * `[data-testid="sidebar-lists"]` — the persistent sidebar and the mobile sheet. On
 * `daiv:runs-changed` (nav-events.js) the store fetches that same partial from the container's
 * `data-url` and replaces each container's rows. Expand state lives here rather than in the
 * rows, so a swap cannot close a schedule; the focused row and the scroll position are carried
 * across it by `data-row-key`.
 *
 *     <button @click="$store.sidebarLists.toggle(7, false)" :aria-expanded="...isOpen(7, false)...">
 *     <ul x-show="$store.sidebarLists.isOpen(7, false)" x-cloak>
 *
 * A refresh that fails — an error status, a redirect to the sign-in page, a body that is not
 * the lists — keeps the rows already shown. Expand choices persist per browser, the scroll
 * position per tab, and "Show more" depth not at all.
 */
document.addEventListener("alpine:init", () => {
  const CHOICES_KEY = "daiv.sidebar.schedules";
  const SCROLL_KEY = "daiv.sidebar.scroll";
  const CONTAINERS = '[data-testid="sidebar-lists"]';
  const LISTS_MARKER = 'data-testid="nav-sessions-group"';
  const PAGE = 8;
  const DEBOUNCE_MS = 400;

  function readChoices() {
    try {
      const choices = JSON.parse(localStorage.getItem(CHOICES_KEY));
      return choices && typeof choices === "object" ? choices : {};
    } catch {
      return {};
    }
  }

  Alpine.store("sidebarLists", {
    limit: PAGE,
    choices: readChoices(),
    _timer: null,
    _inFlight: false,
    _again: false,

    init() {
      window.addEventListener("daiv:runs-changed", () => this.refresh());
    },

    isOpen(scheduleId, startsOpen) {
      const choice = this.choices[scheduleId];
      return typeof choice === "boolean" ? choice : startsOpen;
    },

    toggle(scheduleId, startsOpen) {
      this.choices = { ...this.choices, [scheduleId]: !this.isOpen(scheduleId, startsOpen) };
      try {
        localStorage.setItem(CHOICES_KEY, JSON.stringify(this.choices));
      } catch {
        // Blocked storage only means the next page load starts from the server's default.
      }
    },

    more() {
      this.limit += PAGE;
      this._fetch();
    },

    refresh() {
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this._fetch(), DEBOUNCE_MS);
    },

    restoreScroll(scroller) {
      const top = Number(sessionStorage.getItem(SCROLL_KEY));
      if (top > 0) scroller.scrollTop = top;
    },

    saveScroll(scroller) {
      sessionStorage.setItem(SCROLL_KEY, String(scroller.scrollTop));
    },

    async _fetch() {
      if (this._inFlight) {
        this._again = true;
        return;
      }
      const container = document.querySelector(CONTAINERS);
      if (!container) return;
      this._inFlight = true;
      try {
        const viewing = encodeURIComponent(container.dataset.viewing || "");
        const url = `${container.dataset.url}?limit=${this.limit}&viewing=${viewing}`;
        const response = await fetch(url, { credentials: "same-origin" });
        if (!response.ok || response.redirected) return;
        const html = await response.text();
        if (html.includes(LISTS_MARKER)) this._swap(html);
      } catch {
        // Offline or aborted: the rows already shown stay until the next refresh.
      } finally {
        this._inFlight = false;
        if (this._again) {
          this._again = false;
          this._fetch();
        }
      }
    },

    _swap(html) {
      const focused = document.activeElement?.closest?.("[data-row-key]");
      const focusedIn = focused?.closest(CONTAINERS);
      for (const container of document.querySelectorAll(CONTAINERS)) {
        const scroller = container.closest("[data-sidebar-scroll]");
        const top = scroller?.scrollTop;
        container.innerHTML = html;
        // After Alpine has shown the expanded schedules in the new rows, or the height is short.
        if (scroller) requestAnimationFrame(() => (scroller.scrollTop = top));
        if (container === focusedIn) {
          const key = focused.dataset.rowKey;
          [...container.querySelectorAll("[data-row-key]")].find((row) => row.dataset.rowKey === key)?.focus();
        }
      }
    },
  });
});
