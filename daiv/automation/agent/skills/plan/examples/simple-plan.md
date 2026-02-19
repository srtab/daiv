# Example: Simple Plan — CSS Bug Fix

**User request:** "The dropdown menu in the navbar overlaps the content below it on mobile. Plan a fix."

**Complexity assessment:** Simple. Single visual bug, isolated to CSS, no backend impact.

**Exploration performed:** Read `src/components/Navbar.css`, `src/components/Navbar.jsx`. Grepped for `.dropdown` across the stylesheet. Checked existing z-index values in the project.

---

# Fix: Navbar Dropdown Overlap on Mobile

## What's Changing

The `.dropdown-menu` is missing `z-index` and uses `position: relative` instead of `position: absolute`, causing it to push content down rather than float above it on viewports below 768px.

## Files to Modify

- `src/components/Navbar.css` (line ~83) — change `.dropdown-menu` positioning and add z-index
- `src/components/Navbar.test.jsx` — add snapshot test for mobile dropdown render

## Implementation Notes

- Set `.dropdown-menu` to `position: absolute` and `z-index: 100`. The existing modal uses `z-index: 200`, so 100 is safe.
- The fix only needs to apply inside the `@media (max-width: 768px)` block — desktop is unaffected.
- Check that `overflow: hidden` on the parent `.navbar` container isn't clipping the dropdown. If it is, change to `overflow: visible` (currently line 12).
- No JavaScript changes needed.
