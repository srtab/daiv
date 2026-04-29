(() => {
  const getMarkedParse = () => {
    const m = window.marked;

    if (typeof m?.parse === "function") return m.parse.bind(m);
    if (typeof m === "function") return m;

    return null;
  };

  const ready = () =>
    typeof getMarkedParse() === "function" &&
    typeof window.hljs?.highlightElement === "function" &&
    typeof window.DOMPurify?.sanitize === "function";

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

  const sanitize = (html) =>
    window.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      ADD_ATTR: ["target", "rel", "class"],
      FORBID_TAGS: ["style", "form", "input", "button", "iframe", "object", "embed"],
    });

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
    if (raw == null || raw === "") return "";

    if (!ready()) {
      console.warn("chat-markdown: markdown dependencies not ready", {
        marked: typeof window.marked,
        markedParse: typeof window.marked?.parse,
        hljs: typeof window.hljs,
        highlightElement: typeof window.hljs?.highlightElement,
        DOMPurify: typeof window.DOMPurify,
        sanitize: typeof window.DOMPurify?.sanitize,
      });

      return fallback(raw);
    }

    try {
      const parse = getMarkedParse();

      const html = parse(
        String(raw).replace(/^[\u200B\u200C\u200D\u200E\u200F\uFEFF]/, ""),
        { gfm: true, breaks: true }
      );

      const clean = sanitize(html);
      const highlighted = highlightAll(clean);

      // Keep DOMPurify as the final security boundary.
      return sanitize(highlighted);
    } catch (err) {
      console.warn("chat-markdown: parse failed, using fallback", err);
      return fallback(raw);
    }
  };
})();
