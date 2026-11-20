/**
 * What a sidebar session row's menu does beyond links: copy the session's link, and archive the
 * session once `dashboard/_sidebar_archive_dialog.html` confirms it.
 *
 *     <button @click="$store.sidebarRowActions.askArchive('/api/sessions/<id>/archive')">
 *
 * The toasts' words come from the dialog's `data-*-label` attributes. A confirmed archive
 * refreshes the sidebar's lists, which no longer carry the session.
 */
document.addEventListener("alpine:init", () => {
  const DIALOG_ID = "sidebar-archive-dialog";

  function csrfToken() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || "";
    } catch {
      return "";
    }
  }

  Alpine.store("sidebarRowActions", {
    pendingUrl: null,

    labels() {
      return document.getElementById(DIALOG_ID)?.dataset ?? {};
    },

    async copyLink(path) {
      try {
        await navigator.clipboard.writeText(new URL(path, window.location.href).href);
        window.showToast(this.labels().copiedLabel, "success");
      } catch {
        // Clipboard access denied: Open in new tab still carries the link.
      }
    },

    askArchive(url) {
      this.pendingUrl = url;
      document.getElementById(DIALOG_ID)?.showModal();
    },

    async archive() {
      const url = this.pendingUrl;
      this.pendingUrl = null;
      if (!url) return;
      const { archivedLabel, failedLabel } = this.labels();
      try {
        const response = await fetch(url, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrfToken() },
          body: JSON.stringify({ archived: true }),
        });
        if (!response.ok) throw new Error(`archive failed: ${response.status}`);
        window.showToast(archivedLabel, "success");
        Alpine.store("sidebarLists").refresh();
      } catch {
        window.showToast(failedLabel, "error");
      }
    },
  });
});
