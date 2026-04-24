// Wraps marked + highlight.js into a single `renderMarkdown(raw)` entry point used
// for assistant message text. Both dependencies are loaded from CDN in
// chat_detail.html and attach themselves to `window`.
//
// marked handles GitHub-flavored markdown (code fences, lists, tables, links).
// highlight.js post-processes every <pre><code> it emits for language coloring.

(() => {
  const ready = () => typeof window.marked === "object" && typeof window.hljs === "object";

  const escapeHtml = (s) =>
    String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");

  const fallback = (raw) =>
    String(raw ?? "")
      .split(/\n{2,}/)
      .map((p) => `<p>${escapeHtml(p).replaceAll("\n", "<br>")}</p>`)
      .join("");

  const highlightAll = (html) => {
    const tmpl = document.createElement("template");
    tmpl.innerHTML = html;
    tmpl.content.querySelectorAll("pre > code").forEach((el) => {
      try {
        window.hljs.highlightElement(el);
      } catch (err) {
        console.warn("chat-markdown: highlight failed, leaving plain", err);
      }
    });
    return tmpl.innerHTML;
  };

  window.renderMarkdown = (raw) => {
    if (!raw) return "";
    if (!ready()) return fallback(raw);
    try {
      const html = window.marked.parse(String(raw), { gfm: true, breaks: true });
      return highlightAll(html);
    } catch (err) {
      console.warn("chat-markdown: parse failed, using fallback", err);
      return fallback(raw);
    }
  };
})();
