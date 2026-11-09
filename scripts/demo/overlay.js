(() => {
  const BRAND = "#8B5CF6";
  const ACCENT = "#2DD4BF";
  const INK = "#0B0B14";

  const style = `
    #demo-cursor { position: fixed; z-index: 2147483647; pointer-events: none; width: 22px; height: 22px;
      transform: translate(-3px, -2px); transition: transform 90ms ease-out; }
    #demo-cursor.down { transform: translate(-3px, -2px) scale(0.86); }
    .demo-ripple { position: fixed; z-index: 2147483646; pointer-events: none; width: 36px; height: 36px;
      margin: -18px 0 0 -18px; border-radius: 50%; border: 3px solid ${BRAND};
      animation: demo-ripple 520ms ease-out forwards; }
    @keyframes demo-ripple { from { transform: scale(0.3); opacity: 0.9; } to { transform: scale(1.4); opacity: 0; } }
    #demo-caption { position: fixed; z-index: 2147483645; left: 50%; bottom: 28px; transform: translateX(-50%);
      display: flex; align-items: center; gap: 14px; padding: 12px 22px 12px 12px; border-radius: 999px;
      background: ${INK}; color: #fff; font: 600 21px/1.2 Inter, "Segoe UI", system-ui, sans-serif;
      box-shadow: 0 10px 30px rgba(0,0,0,0.28); white-space: nowrap; transition: opacity 250ms; }
    #demo-caption .step { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 50%;
      background: ${BRAND}; font-size: 18px; }
    #demo-caption code { font: 600 19px/1 ui-monospace, "SFMono-Regular", monospace; color: ${ACCENT};
      background: transparent; padding: 0; border: 0; }
    #demo-badge { position: fixed; z-index: 2147483645; top: 22px; left: 50%; transform: translateX(-50%);
      padding: 9px 18px; border-radius: 999px; background: ${BRAND}; color: #fff;
      font: 600 18px/1 Inter, "Segoe UI", system-ui, sans-serif; box-shadow: 0 8px 24px rgba(0,0,0,0.25);
      animation: demo-badge 2600ms ease-in-out forwards; }
    @keyframes demo-badge { 0% { opacity: 0; } 10% { opacity: 1; } 80% { opacity: 1; } 100% { opacity: 0; } }
  `;

  const cursorSvg = `<svg viewBox="0 0 22 22" width="22" height="22" xmlns="http://www.w3.org/2000/svg">
    <path d="M3 2 L3 18 L7.5 13.8 L10.6 20.4 L13.3 19.2 L10.3 12.7 L16.5 12.7 Z"
      fill="#111" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/></svg>`;

  function mount() {
    if (document.getElementById("demo-style")) return;
    const s = document.createElement("style");
    s.id = "demo-style";
    s.textContent = style;
    document.head.appendChild(s);
    const c = document.createElement("div");
    c.id = "demo-cursor";
    c.innerHTML = cursorSvg;
    const last = window.__demoCursor || { x: 640, y: 360 };
    c.style.left = `${last.x}px`;
    c.style.top = `${last.y}px`;
    document.body.appendChild(c);
  }

  document.addEventListener("mousemove", (e) => {
    window.__demoCursor = { x: e.clientX, y: e.clientY };
    const c = document.getElementById("demo-cursor");
    if (c) { c.style.left = `${e.clientX}px`; c.style.top = `${e.clientY}px`; }
  }, true);
  document.addEventListener("mousedown", (e) => {
    document.getElementById("demo-cursor")?.classList.add("down");
    const r = document.createElement("div");
    r.className = "demo-ripple";
    r.style.left = `${e.clientX}px`;
    r.style.top = `${e.clientY}px`;
    document.body.appendChild(r);
    setTimeout(() => r.remove(), 600);
  }, true);
  document.addEventListener("mouseup", () => document.getElementById("demo-cursor")?.classList.remove("down"), true);

  function ease(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }

  window.__demo = {
    caption(step, text) {
      mount();
      let el = document.getElementById("demo-caption");
      if (!el) { el = document.createElement("div"); el.id = "demo-caption"; document.body.appendChild(el); }
      el.innerHTML = `<span class="step">${step}</span><span>${text}</span>`;
    },
    badge(text) {
      mount();
      document.getElementById("demo-badge")?.remove();
      const el = document.createElement("div");
      el.id = "demo-badge";
      el.textContent = `⏩  ${text}`;
      document.body.appendChild(el);
    },
    scroller() {
      const root = document.scrollingElement;
      if (root.scrollHeight > root.clientHeight + 4) return root;
      let best = root, area = 0;
      for (const el of document.querySelectorAll("body *")) {
        if (el.scrollHeight <= el.clientHeight + 4) continue;
        const oy = getComputedStyle(el).overflowY;
        if (oy !== "auto" && oy !== "scroll") continue;
        const a = el.clientWidth * el.clientHeight;
        if (a > area) { area = a; best = el; }
      }
      return best;
    },
    topOf(el) {
      const s = this.scroller();
      const base = s === document.scrollingElement ? 0 : s.getBoundingClientRect().top;
      return el.getBoundingClientRect().top - base + s.scrollTop;
    },
    smoothScroll(y, duration) {
      const s = this.scroller(), start = s.scrollTop, delta = y - start, t0 = performance.now();
      if (duration <= 20) { s.scrollTop = y; return; }
      const step = (now) => {
        const t = Math.min((now - t0) / duration, 1);
        s.scrollTop = start + delta * ease(t);
        if (t < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },
    smoothScrollBy(dy, duration) { this.smoothScroll(this.scroller().scrollTop + dy, duration); },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
