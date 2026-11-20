/**
 * Session actions every signed-in page shares: the sessions API request, copying a link, and archiving through the
 * one `dashboard/_session_archive_dialog.html`, which carries the CSRF token and the toasts' words.
 *
 *     <button @click="$store.sessionActions.askArchive(() => $store.sessionActions.archive(url))">
 *
 * `askArchive` runs its callback once the dialog confirms; `archive` is the sidebar's, which refreshes its lists.
 */
document.addEventListener("alpine:init", () => {
  const DIALOG_ID = "session-archive-dialog";

  Alpine.store("sessionActions", {
    onConfirm: null,

    config() {
      return document.getElementById(DIALOG_ID)?.dataset ?? {};
    },

    send(url, method, body) {
      return fetch(url, {
        method,
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": this.config().csrfToken ?? "" },
        body: JSON.stringify(body),
      });
    },

    async copyLink(path) {
      try {
        await navigator.clipboard.writeText(new URL(path, window.location.href).href);
        window.showToast(this.config().copiedLabel, "success");
      } catch {
        // Clipboard access denied: Open in new tab still carries the link.
      }
    },

    askArchive(onConfirm) {
      this.onConfirm = onConfirm;
      document.getElementById(DIALOG_ID)?.showModal();
    },

    confirmArchive() {
      const onConfirm = this.onConfirm;
      this.onConfirm = null;
      onConfirm?.();
    },

    async archive(url) {
      const { archivedLabel, failedLabel } = this.config();
      try {
        const response = await this.send(url, "POST", { archived: true });
        if (!response.ok) throw new Error(`archive failed: ${response.status}`);
        window.showToast(archivedLabel, "success");
        Alpine.store("sidebarLists").refresh();
      } catch {
        window.showToast(failedLabel, "error");
      }
    },
  });
});
