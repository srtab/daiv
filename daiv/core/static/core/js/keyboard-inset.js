/**
 * How much of the viewport bottom the software keyboard is covering, published as
 * `--keyboard-inset` on `:root` for CSS to give up.
 *
 * A phone keyboard never resizes the *layout* viewport on iOS — Safari implements no
 * `interactive-widget`, so `100dvh`, `height: 100%` and every viewport-anchored box keep
 * the height they had, and the keyboard is simply drawn on top of the bottom of the page.
 * That is why a bottom-docked composer ends up in the wrong place the moment it is
 * focused: it is anchored to a viewport edge that is no longer visible (`position: sticky`
 * gets no exemption — WebKit bug 202120), and Safari's own attempt to rescue the focused
 * field by panning the visual viewport up is measured against that same stale layout.
 *
 * The keyboard's height shows up in exactly one place, the visual viewport, so measure it
 * there and let CSS shrink whatever has to stay above it (`main:has(.chat-shell)` in
 * input.css). Zero on a desktop browser, which is why nothing here is feature-gated: the
 * inset is 0 unless something really is covering the page.
 */
(() => {
  const viewport = window.visualViewport;
  if (!viewport) return;

  const root = document.documentElement;
  let published = null;
  let queued = false;

  const measure = () => {
    // Pinch-zoom shrinks the visual viewport the same way a keyboard does, and there the
    // page bottom is off screen because the reader put it there.
    if (viewport.scale > 1) return 0;
    // Safari pans the page up to lift the focused field off the keyboard instead of
    // resizing anything; `offsetTop` is how far it got. Subtracting it leaves only the
    // part of the keyboard that pan has not already covered, so the two compose rather
    // than stack — full pan, no inset; no pan, the whole keyboard.
    return Math.max(0, Math.round(window.innerHeight - viewport.height - viewport.offsetTop));
  };

  const publish = () => {
    queued = false;
    const inset = measure();
    if (inset === published) return;
    published = inset;
    root.style.setProperty("--keyboard-inset", `${inset}px`);
  };

  // Coalesced: the pan arrives as a stream of visual-viewport scrolls, and every write
  // here resizes a scroll container.
  const queuePublish = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(publish);
  };

  viewport.addEventListener("resize", queuePublish);
  viewport.addEventListener("scroll", queuePublish);
  publish();
})();
