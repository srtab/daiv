# Code Review — Few-Shot Examples

Each section shows one archetype rendered in several languages — comment / fix
pairs for fix archetypes, or comment-only snippets for the `question`
archetype (which carries no fix). Treat these as *shape* references: the
archetype generalizes; the syntax is language-specific. Use them to calibrate
how short an inline review comment can be, what a suggestion block typically
replaces, what a yes/no question looks like, and how the same defect manifests
across stacks. These are not templates to copy verbatim.

Part 1 collects the **inline-eligible** archetypes — the four fix archetypes
(`remove_dead_lines`, `use_framework_idiom`, `replace_with_constant`,
`swap_library_call`), which ship a `suggestion` block, plus `question`, which
anchors a question on a new-side line with no `suggestion`. Part 2 collects the
**discussion-only** archetypes, which deliver as a summary `discussion`.
`gitlab-delivery.md` Step 3 is authoritative on the inline-vs-summary split
(including why a `rename` can't be inline); these are just the shape examples.

---

# Part 1 — Inline-eligible archetypes

These ship a `suggestion` block (or, for `question`, an anchored question). The exact suggestion syntax is delivery-critical, so each language a team uses is calibrated here.

## `remove_dead_lines` (inline-eligible)

Dead lines are statements reachable by no execution path and declarations whose value is never consumed. The smell recurs across languages: a variable assigned and immediately overwritten, a parameter accepted but ignored, a flag toggled back to its default, or a commented-out block left in after a refactor. The fix is deletion with no behavioural change.

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

The diff reimplements something the language's standard library or the surrounding runtime already ships as a single expression. The hand-rolled version adds lines, introduces edge-case risk, and signals unfamiliarity with the platform. The fix is to swap in the canonical one-liner.

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

A raw literal — number, string, or arithmetic expression — is used inline instead of a named constant. The smell is either repetition (the same value appears in two or more places) or opacity (the value has a non-obvious domain meaning, like `86400` for seconds-in-a-day). The fix is a one-line extraction to a named constant at module or package scope, after which every use site becomes self-documenting and the value has a single source of truth.

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

func truncateDisplay(name string) string {
    if len(name) > 32 {
        return name[:32] + "…"
    }
    return name
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

func truncateDisplay(name string) string {
    if len(name) > maxUsernameLength {
        return name[:maxUsernameLength] + "…"
    }
    return name
}
```

---

## `swap_library_call` (inline-eligible)

A call to a standard-library or well-known library function that is technically functional but is the wrong API for the job: it may block when an async alternative exists, mutate when a copy-producing variant is available, use a deprecated entry point, or ignore locale/timezone context that the correct call handles automatically. The fix is a one-line substitution — no restructuring needed.

### Example — TypeScript

**Reviewer comment:** `\`sort()\` mutates the original array — use \`toSorted()\` or spread first.`

**Before:**

```typescript
function getTopScorers(players: Player[]): Player[] {
    return players
        .sort((a, b) => b.score - a.score)
        .slice(0, 10);
}

// Caller still expects `players` to be in original order after this call.
renderLeaderboard(getTopScorers(roster));
renderRoster(roster); // silently broken — roster is now sorted by score
```

**After:**

```typescript
function getTopScorers(players: Player[]): Player[] {
    return players
        .toSorted((a, b) => b.score - a.score)
        .slice(0, 10);
}

renderLeaderboard(getTopScorers(roster));
renderRoster(roster); // roster order is preserved
```

---

## `question` (inline-eligible)

A targeted question anchored on a single new-side line or a contiguous
new-side range within one hunk. The reader needs the author's *intent*, not
a fix — does the diff really do what it looks like it does, or is there a
hidden constraint the comment doesn't capture? Inline delivery puts the
question on the exact line(s) in the diff view, so the author can answer
with context instead of hunting through a summary. The body is marker + one
or two sentences ending in `?` — no `suggestion` block. When the question
is about a multi-line block, scope the discussion position to the full range
and compute the anchor on the first new-side line of that range.

The bar is high: don't ask curiosity questions, don't paraphrase the code,
don't ask things the diff itself already answers. The hypothesis must be
concrete and yes/no-answerable.

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

The question is about the block as a whole, not a single line. Scope the discussion position to the contiguous range; the anchor is computed on the first new-side line of the range (`try {` here).

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

These all serialize as the `discussion` archetype — prose findings in the summary, no inline `suggestion` block. The names below are recognition aids, **not** schema values (`review-workflow.md`: only the six schema strings are valid). Carrying no `suggestion` block, they need no per-language syntax calibration — the table is the reference; two worked examples follow to show the before/after prose shape.

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

A name in the diff actively misleads the reader: it implies a type, scope, or concept that does not match what the identifier holds. The fix is a targeted rename — no logic changes — but it propagates to every call site, so it goes in the summary, not an inline suggestion.

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

## Example — `move_to_other_module` (Go)

Code placed in the wrong architectural layer — here, password hashing inside an HTTP handler. The fix creates a function in the layer that owns the concern and updates the call site, so it spans two files and can't be one inline suggestion.

**Reviewer comment:** `Password hashing belongs in the \`user\` package, not the HTTP handler.`

**Before** (`handler/auth.go`):

```go
func RegisterHandler(w http.ResponseWriter, r *http.Request) {
    email := r.FormValue("email")
    password := r.FormValue("password")

    hash, err := bcrypt.GenerateFromPassword([]byte(password), 12)
    if err != nil {
        http.Error(w, "internal error", http.StatusInternalServerError)
        return
    }
    if err := store.CreateUser(email, string(hash)); err != nil {
        http.Error(w, "could not create user", http.StatusBadRequest)
        return
    }
    w.WriteHeader(http.StatusCreated)
}
```

**After** (`user/user.go` — new function, and `handler/auth.go` calls `user.HashPassword(password)`):

```go
const bcryptCost = 12

func HashPassword(plain string) (string, error) {
    b, err := bcrypt.GenerateFromPassword([]byte(plain), bcryptCost)
    if err != nil {
        return "", err
    }
    return string(b), nil
}
```
