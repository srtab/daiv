# Code Review — Few-Shot Examples

Each section shows one archetype rendered in several languages — comment / fix
pairs for fix archetypes, or comment-only snippets for the `question`
archetype (which carries no fix). Treat these as *shape* references: the
archetype generalizes; the syntax is language-specific. Use them to calibrate
how short an inline review comment can be, what a suggestion block typically
replaces, what a yes/no question looks like, and how the same defect manifests
across stacks. These are not templates to copy verbatim.

Inline-eligible archetypes come in two shapes. **Fix archetypes** ship a
`suggestion` block: `remove_dead_lines`, `use_framework_idiom`,
`replace_with_constant`, `swap_library_call`. The **question archetype**
(`question`) is also inline-eligible: it anchors on a specific new-side line
but carries no `suggestion` block — just the question. Everything else —
including `rename`, which propagates to call sites that a single-line
suggestion can't patch, and cross-file or un-anchored questions — goes in
the summary discussion.

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

These deliver as the `discussion` archetype in the summary — prose findings, no inline `suggestion` block.

## `rename` (discussion-only)

A name in the diff actively misleads the reader: it implies a type, scope, or concept that does not match what the identifier actually holds. The fix is a targeted rename — no logic changes. The bar for this archetype is a name that would send a future reader in the wrong direction, not just one that could be marginally clearer.

### Example — Python

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

---

## `move_to_other_module` (discussion-only)

Code placed in the wrong architectural layer: a template or view doing data transformation that belongs in the model/service layer, a route handler encoding business rules that belong in a domain object, or an entry-point module accumulating logic that belongs in a dedicated module. Because the fix requires creating or extending a second file and updating the call site, this finding cannot be expressed as a single inline suggestion — it belongs in the review summary.

### Example — Go

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

**After** (`user/user.go` — new function):

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

**After** (`handler/auth.go` — simplified call site):

```go
func RegisterHandler(w http.ResponseWriter, r *http.Request) {
    email := r.FormValue("email")
    password := r.FormValue("password")

    hash, err := user.HashPassword(password)
    if err != nil {
        http.Error(w, "internal error", http.StatusInternalServerError)
        return
    }
    if err := store.CreateUser(email, hash); err != nil {
        http.Error(w, "could not create user", http.StatusBadRequest)
        return
    }
    w.WriteHeader(http.StatusCreated)
}
```

---

## `extract_to_helper` (discussion-only)

This archetype appears when the same multi-step operation is copy-pasted two or more times across a file or module. The duplication is structural: the same sequence of steps, usually parameterised only by a small set of values, repeats verbatim. The fix is to give that sequence a name and collapse each call site to one line. Future call sites then get the named helper for free, and any change to the logic happens in one place.

### Example — TypeScript

**Reviewer comment:** The retry-with-delay loop appears in two places already. Extract it to `withRetry` before a third copy lands.

**Before:**

```typescript
async function fetchWithRetry(url: string): Promise<Response> {
    let attempt = 0;
    while (attempt < 3) {
        try {
            return await fetch(url);
        } catch {
            attempt++;
            await new Promise(r => setTimeout(r, attempt * 200));
        }
    }
    throw new Error(`Failed after 3 attempts: ${url}`);
}

async function postWithRetry(url: string, body: unknown): Promise<Response> {
    let attempt = 0;
    while (attempt < 3) {
        try {
            return await fetch(url, { method: "POST", body: JSON.stringify(body) });
        } catch {
            attempt++;
            await new Promise(r => setTimeout(r, attempt * 200));
        }
    }
    throw new Error(`Failed after 3 attempts: ${url}`);
}
```

**After:**

```typescript
async function withRetry<T>(fn: () => Promise<T>, maxAttempts = 3): Promise<T> {
    let attempt = 0;
    while (attempt < maxAttempts) {
        try {
            return await fn();
        } catch {
            attempt++;
            await new Promise(r => setTimeout(r, attempt * 200));
        }
    }
    throw new Error(`Failed after ${maxAttempts} attempts`);
}

async function fetchWithRetry(url: string): Promise<Response> {
    return withRetry(() => fetch(url));
}

async function postWithRetry(url: string, body: unknown): Promise<Response> {
    return withRetry(() => fetch(url, { method: "POST", body: JSON.stringify(body) }));
}
```

---

## `extract_to_base_or_shared_type` (discussion-only)

This archetype appears when two or more distinct types carry identical fields or methods — usually lifecycle fields (`created_at`, `updated_at`, `id`), capability groups (`Validate() error`, `Name() string`), or a common wire shape. Each language has its own mechanism — Python uses a base class, Go uses struct embedding, TypeScript uses an interface implemented by multiple classes — but the smell is the same: the shape is copied rather than named. The fix is to lift the shared shape into one definition that all types reference, so any future change or addition to it propagates automatically.

> Note: This archetype is the language-agnostic generalisation of language-specific patterns like mixin, embedded struct, trait with default impl, and interface + abstract class.

### Example — Python

**Reviewer comment:** `id`, `created_at`, and `updated_at` appear on every model. Lift them into a `TimestampedModel` base so new models get them for free and the migration surface stays small.

**Before:**

```python
class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(onupdate=func.now())
    title: Mapped[str]
    body: Mapped[str]


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(onupdate=func.now())
    content: Mapped[str]
    author_id: Mapped[int]
```

**After:**

```python
class TimestampedModel(Base):
    __abstract__ = True

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(onupdate=func.now())


class Article(TimestampedModel):
    __tablename__ = "articles"
    title: Mapped[str]
    body: Mapped[str]


class Comment(TimestampedModel):
    __tablename__ = "comments"
    content: Mapped[str]
    author_id: Mapped[int]
```

---

## `split_responsibility` (discussion-only)

This archetype appears when a single function, class, or module is doing two clearly orthogonal jobs — for example, fetching data and transforming it, or validating input and writing audit logs. The tell is that the unit has two reasons to change: once for each responsibility. Reviewers often spot it when the function name uses "and", when a class has method groups with no shared state, or when a unit is hard to test because mocking one concern forces you to stub the other. The fix is to separate the two responsibilities into distinct units and compose them at the call site.

### Example — Go

**Reviewer comment:** `ProcessOrder` is doing validation and persistence in one pass. These change for different reasons — split so each can be tested and evolved independently.

**Before:**

```go
func ProcessOrder(db *sql.DB, o Order) error {
    if o.Quantity <= 0 {
        return errors.New("quantity must be positive")
    }
    if o.Price < 0 {
        return errors.New("price must be non-negative")
    }
    if o.CustomerID == "" {
        return errors.New("customer id is required")
    }

    _, err := db.Exec(
        `INSERT INTO orders (customer_id, quantity, price) VALUES ($1, $2, $3)`,
        o.CustomerID, o.Quantity, o.Price,
    )
    return err
}
```

**After:**

```go
func ValidateOrder(o Order) error {
    if o.Quantity <= 0 {
        return errors.New("quantity must be positive")
    }
    if o.Price < 0 {
        return errors.New("price must be non-negative")
    }
    if o.CustomerID == "" {
        return errors.New("customer id is required")
    }
    return nil
}

func SaveOrder(db *sql.DB, o Order) error {
    _, err := db.Exec(
        `INSERT INTO orders (customer_id, quantity, price) VALUES ($1, $2, $3)`,
        o.CustomerID, o.Quantity, o.Price,
    )
    return err
}

// caller
func ProcessOrder(db *sql.DB, o Order) error {
    if err := ValidateOrder(o); err != nil {
        return err
    }
    return SaveOrder(db, o)
}
```

---

## `add_guard` (discussion-only)

This archetype appears whenever code reaches an operation that requires a precondition — unwrapping an optional, using a value from external input, dereferencing a pointer — without first verifying that precondition holds. The trust boundary is the key: data arriving from outside the current module (HTTP request, database row, user argument, network response) can be absent, malformed, or out of range. The fix is a small early-return or error-return that rejects the bad input before it propagates deeper.

### Example — TypeScript

**Reviewer comment:** `payload.expiresAt` is parsed from JSON — it could be `undefined` or an invalid date string. Guard before using it.

**Before:**

```typescript
async function scheduleTask(payload: Record<string, unknown>): Promise<void> {
    const expiresAt = new Date(payload.expiresAt as string);
    await db.tasks.create({
        data: {
            name: payload.name as string,
            expiresAt,
        },
    });
}
```

**After:**

```typescript
async function scheduleTask(payload: Record<string, unknown>): Promise<void> {
    if (!payload.expiresAt || typeof payload.expiresAt !== "string") {
        throw new Error("expiresAt is required and must be a string");
    }
    const expiresAt = new Date(payload.expiresAt);
    if (isNaN(expiresAt.getTime())) {
        throw new Error(`expiresAt is not a valid date: ${payload.expiresAt}`);
    }
    if (!payload.name || typeof payload.name !== "string") {
        throw new Error("name is required and must be a string");
    }
    await db.tasks.create({
        data: {
            name: payload.name,
            expiresAt,
        },
    });
}
```

---

## `add_invariant_raise` (discussion-only)

This archetype appears when the code defensively handles a state that the surrounding logic guarantees cannot occur — for example, returning silently when a value is `None` on a code path that only runs after the value has been confirmed non-`None`, or `default`-ing a switch branch that covers every valid enum variant. Silent no-ops in impossible branches hide bugs: when the invariant is actually violated (due to a future refactor or a caller mistake), the program continues in a corrupt state instead of failing loudly. The fix is to replace the silent fallback with an explicit raise/panic/error, turning a masked bug into a visible one.

### Example — Python

**Reviewer comment:** If `current_user` is `None` here the middleware already failed. Silent `return` hides the bug — raise instead.

**Before:**

```python
def transfer_funds(amount: Decimal, to_account_id: int) -> None:
    # Middleware guarantees current_user is set before this handler runs
    current_user = get_current_user()
    if current_user is None:
        return  # should never happen
    if amount <= 0:
        raise ValueError("amount must be positive")
    ledger.debit(current_user.account_id, amount)
    ledger.credit(to_account_id, amount)
```

**After:**

```python
def transfer_funds(amount: Decimal, to_account_id: int) -> None:
    # Middleware guarantees current_user is set before this handler runs
    current_user = get_current_user()
    if current_user is None:
        raise RuntimeError("Invariant violated: current_user is None inside an authenticated handler")
    if amount <= 0:
        raise ValueError("amount must be positive")
    ledger.debit(current_user.account_id, amount)
    ledger.credit(to_account_id, amount)
```

---

## `fix_logic_bug` (discussion-only)

This archetype covers code that computes the wrong result on ordinary inputs: an off-by-one that skips the last element, an inverted condition that rejects valid input instead of invalid, a `=` that resets an accumulator instead of `+=` that builds it, or an index that reads from the wrong position in a loop. These bugs are usually subtle — the code looks plausible at a glance — but they produce consistently wrong outputs that tests or careful review expose. The after must show the correct logic, not a comment or a TODO.

### Example — Go

**Reviewer comment:** `total = price * qty` resets `total` on every iteration — the earlier lines are thrown away. This must be `total +=`.

**Before:**

```go
func calculateInvoiceTotal(lines []LineItem) float64 {
    var total float64
    for _, line := range lines {
        price := line.UnitPrice * (1 - line.DiscountRate)
        qty := float64(line.Quantity)
        total = price * qty
    }
    return total
}
```

**After:**

```go
func calculateInvoiceTotal(lines []LineItem) float64 {
    var total float64
    for _, line := range lines {
        price := line.UnitPrice * (1 - line.DiscountRate)
        qty := float64(line.Quantity)
        total += price * qty
    }
    return total
}
```
