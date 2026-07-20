# Code Review — Few-Shot Examples

Each section shows one archetype rendered in a representative language —
comment/fix pairs for fix archetypes, comment-only for `question` (no fix).
Treat these as *shape* references, not templates to copy verbatim: they
calibrate how short a comment can be, what a suggestion block replaces, and
how the same defect manifests across stacks. Part 1 collects the
**inline-eligible** archetypes — the four fix archetypes (`remove_dead_lines`,
`use_framework_idiom`, `replace_with_constant`, `swap_library_call`), which
ship a `suggestion` block, plus `question`, which anchors on a new-side line
with no `suggestion` — while Part 2 collects the **discussion-only**
archetypes, delivered as summary prose; `gitlab-delivery.md` Step 3 is
authoritative on the inline-vs-summary split (including why a `rename` can't
be inline) — these are just the shape examples.

---

# Part 1 — Inline-eligible archetypes

These ship a `suggestion` block (or, for `question`, an anchored question) — the exact syntax is delivery-critical.

## `remove_dead_lines` (inline-eligible)

Dead lines are statements reachable by no execution path, or declarations whose value is never consumed — an ignored parameter, a stale flag, a leftover commented-out block. The fix is deletion with no behavioural change.

### Example — C#

**Reviewer comment:** `verbose is accepted but no call site passes it; drop the parameter.`

**Before:**

```csharp
public Index BuildIndex(IEnumerable<Item> items, bool verbose)
{
    var idx = new Index();
    foreach (var item in items)
    {
        idx.Add(item.Key, item.Value);
    }
    return idx;
}
```

**After:**

```csharp
public Index BuildIndex(IEnumerable<Item> items)
{
    var idx = new Index();
    foreach (var item in items)
    {
        idx.Add(item.Key, item.Value);
    }
    return idx;
}
```

---

## `use_framework_idiom` (inline-eligible)

The diff reimplements something the standard library already ships as a single expression, adding lines and edge-case risk for no benefit. The fix is to swap in the canonical one-liner.

### Example — PHP

**Reviewer comment:** `http_build_query builds this in one call; drop the manual loop.`

**Before:**

```php
function buildQueryString(array $params): string
{
    $parts = [];
    foreach ($params as $key => $value) {
        $parts[] = urlencode($key) . '=' . urlencode($value);
    }
    return implode('&', $parts);
}
```

**After:**

```php
function buildQueryString(array $params): string
{
    return http_build_query($params);
}
```

---

## `replace_with_constant` (inline-eligible)

A raw literal is used inline instead of a named constant — either repeated across sites or opaque in meaning (e.g. `86400` for seconds-in-a-day). The fix is a one-line extraction to a named constant at module or package scope.

### Example — Go

**Reviewer comment:** `Repeated literal — define a package-level const.`

**Before:**

```go
func validateUsername(name string) error {
    if len(name) < 3 {
        return errors.New("username too short")
    }
    if len(name) > 32 {
        return errors.New("username too long")
    }
    return nil
}
```

**After:**

```go
const maxUsernameLength = 32
const minUsernameLength = 3

func validateUsername(name string) error {
    if len(name) < minUsernameLength {
        return errors.New("username too short")
    }
    if len(name) > maxUsernameLength {
        return errors.New("username too long")
    }
    return nil
}
```

---

## `swap_library_call` (inline-eligible)

A library call is technically functional but the wrong API for the job — it may block, mutate, or use a deprecated entry point when a better alternative exists. The fix is a one-line substitution, no restructuring needed.

### Example — TypeScript

**Reviewer comment:** `\`sort()\` mutates the original array — use \`toSorted()\` or spread first.`

**Before:**

```typescript
function getTopScorers(players: Player[]): Player[] {
    return players
        .sort((a, b) => b.score - a.score)
        .slice(0, 10);
}
```

**After:**

```typescript
function getTopScorers(players: Player[]): Player[] {
    return players
        .toSorted((a, b) => b.score - a.score)
        .slice(0, 10);
}
```

---

## `question` (inline-eligible)

A targeted question anchored on a new-side line (or, for a multi-line block, the full range with the anchor on its first new-side line) — the body is marker + one or two sentences ending in `?`, seeking the author's intent rather than proposing a fix. The bar is high: no curiosity questions or paraphrasing — only a concrete, yes/no-answerable hypothesis.

### Example — Python

**Reviewer comment:** `signal.connect with weak=False is called on every form __init__ — does this leak a handler per form instance, or is the signal expected to be re-registered on each request?`

**Code:**

```python
class OrderForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        post_save.connect(self._update_inventory, sender=Order, weak=False)
```

### Example — multi-line block (TypeScript)

Scope the discussion position to the contiguous range; the anchor is the first new-side line (`try {` here).

**Reviewer comment:** `This catch swallows network errors, parse errors, and validation errors under one branch and returns the same fallback. Is collapsing all three intentional, or should at least validation errors surface to the caller?`

**Code (lines 24–31, single hunk):**

```typescript
try {
    const resp = await fetch(url);
    const json = await resp.json();
    return schema.parse(json);
} catch {
    return DEFAULT_CONFIG;
}
```

---

# Part 2 — Discussion-only archetypes

These all serialize as the `discussion` archetype — prose findings in the summary, no inline `suggestion` block, so no per-language syntax calibration is needed. The names below are recognition aids, **not** schema values (`review-workflow.md`: only the six schema strings are valid) — the table is the reference; one worked example follows to show the before/after prose shape.

| Archetype | The tell | Why it's discussion-only |
|---|---|---|
| `rename` | A name implies a type/scope/concept that doesn't match what it holds | The rename propagates to call sites a single-line suggestion can't patch |
| `move_to_other_module` | Logic in the wrong layer (a view doing data work, a handler encoding business rules) | The fix creates/extends a second file and updates the call site |
| `extract_to_helper` | The same multi-step sequence is copy-pasted 2+ times | Extracting it and rewiring each call site spans non-adjacent hunks |
| `extract_to_base_or_shared_type` | Two+ types carry identical fields/methods (lifecycle fields, a common wire shape) | Lifting the shared shape touches every type that adopts it |
| `split_responsibility` | One unit does two orthogonal jobs (name often has "and"; method groups share no state) | Separating the concerns restructures the unit and its callers |
| `add_guard` | An operation runs on external input without checking a precondition (unwrap, deref, parse) | The guard is justified in prose at a trust boundary, not a mechanical line swap |
| `add_invariant_raise` | Code silently no-ops a state the surrounding logic says cannot occur | Replacing the silent fallback with a raise needs the invariant explained |
| `fix_logic_bug` | Code computes the wrong result on ordinary inputs (off-by-one, inverted condition, `=` vs `+=`) | No inline fix archetype matches a general logic correction |

## Example — `rename` (Python)

A name actively misleads the reader — it implies a type/scope/concept the identifier doesn't hold. The rename itself is trivial, but propagating it to every call site puts this in the summary, not an inline suggestion.

**Reviewer comment:** `is_admin is a User object, not a bool; name it user or admin_user.`

**Before:**

```python
def revoke_access(session_id: str) -> None:
    is_admin = get_user_by_session(session_id)
    if is_admin is None:
        raise ValueError("Session not found")
    if not is_admin.has_role("admin"):
        raise PermissionError("Insufficient privileges")
    is_admin.revoke_all_tokens()
```

**After:**

```python
def revoke_access(session_id: str) -> None:
    admin_user = get_user_by_session(session_id)
    if admin_user is None:
        raise ValueError("Session not found")
    if not admin_user.has_role("admin"):
        raise PermissionError("Insufficient privileges")
    admin_user.revoke_all_tokens()
```
