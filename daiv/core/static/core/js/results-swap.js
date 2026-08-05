/**
 * Shared results-only swap core for filtered list views (sessions, memory).
 *
 * createResultsSwap(containerId, { errorMessage, onUrlChanged }) returns { swapResults }.
 * The filter bar lives OUTSIDE the swapped container, so it never jumps. History is
 * manual: swapResults() pushState()s the new URL only after the results actually swap
 * in, so the address bar can never describe results the user isn't seeing.
 */
window.createResultsSwap = function createResultsSwap(containerId, { errorMessage, onUrlChanged } = {}) {
    // Monotonic swap token: only the newest swap may commit the URL and clear the loading
    // state, so overlapping requests (fast clicks, a debounced apply landing near a click)
    // can't have a stale one win.
    let swapSeq = 0;

    function swapResults(url, { push = true } = {}) {
        const box = document.getElementById(containerId);
        if (!box) return;
        const seq = ++swapSeq;
        box.classList.add("results--loading");

        // A swap only happens on a 2xx; a 4xx/5xx or network error leaves the old content
        // in place. htmx.ajax's promise resolves in all of those cases, so detect a REAL
        // swap via the swap event.
        let swapped = false;
        const onSwap = () => {
            swapped = true;
        };
        box.addEventListener("htmx:afterSwap", onSwap, { once: true });

        htmx
            .ajax("GET", url, { target: `#${containerId}`, swap: "innerHTML" })
            .catch(() => {}) // network/send error: no swap fired, handled below
            .finally(() => {
                box.removeEventListener("htmx:afterSwap", onSwap);
                if (seq !== swapSeq) return; // a newer swap superseded this one — let it win
                box.classList.remove("results--loading");
                if (swapped) {
                    if (push) window.history.pushState({}, "", url);
                } else {
                    window.showToast(errorMessage, "error");
                }
            });
    }

    // Pagination links inside the results fragment are re-rendered on every swap; a
    // delegated listener keeps working across swaps without re-binding.
    document.addEventListener("click", (event) => {
        const link = event.target.closest(`#${containerId} a[data-page-swap]`);
        if (!link) return;
        // Preserve open-in-new-tab/window: only hijack an unmodified primary-button click.
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        swapResults(link.getAttribute("href"));
    });

    // Back/forward: re-fetch for the popped URL (no extra push) and re-sync the bar.
    window.addEventListener("popstate", () => {
        swapResults(window.location.pathname + window.location.search, { push: false });
        if (onUrlChanged) onUrlChanged();
    });

    return { swapResults };
};
