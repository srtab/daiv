# Code Review — Few-Shot Examples

Each section shows one fix archetype rendered in three different languages.
Treat these as *shape* references: the archetype generalizes; the syntax is
language-specific. Use them to calibrate how short an inline review comment
can be, what a suggestion block typically replaces, and how the same defect
manifests across stacks. These are not templates to copy verbatim.

Inline-eligible archetypes (the skill posts these as inline suggestion blocks):
`remove_dead_lines`, `use_framework_idiom`, `replace_with_constant`,
`swap_library_call`. Everything else — including `rename`, which propagates to
call sites that a single-line suggestion can't patch — goes in the summary
discussion.

---

## `remove_dead_lines` (inline-eligible)

Dead lines are statements reachable by no execution path and declarations whose value is never consumed. The smell recurs across languages: a variable assigned and immediately overwritten, a parameter accepted but ignored, a flag toggled back to its default, or a commented-out block left in after a refactor. The fix is deletion with no behavioural change.

### Example A — Python

**Reviewer comment:** `user_count is set but never read; drop it.`

**Before:**

```python
def summarise_report(records: list[dict]) -> str:
    total = len(records)
    user_count = 0  # assigned but never used
    lines = []
    for rec in records:
        lines.append(f"{rec['name']}: {rec['value']}")
    summary = "\n".join(lines)
    return f"Total: {total}\n{summary}"
```

**After:**

```python
def summarise_report(records: list[dict]) -> str:
    total = len(records)
    lines = []
    for rec in records:
        lines.append(f"{rec['name']}: {rec['value']}")
    summary = "\n".join(lines)
    return f"Total: {total}\n{summary}"
```

### Example B — Go

**Reviewer comment:** `verbose is threaded in but no call site passes true; remove the param.`

**Before:**

```go
func BuildIndex(items []Item, verbose bool) *Index {
    idx := &Index{}
    for _, item := range items {
        idx.Add(item.Key, item.Value)
    }
    return idx
}

func main() {
    idx := BuildIndex(loadItems(), false)
    _ = idx
}
```

**After:**

```go
func BuildIndex(items []Item) *Index {
    idx := &Index{}
    for _, item := range items {
        idx.Add(item.Key, item.Value)
    }
    return idx
}

func main() {
    idx := BuildIndex(loadItems())
    _ = idx
}
```

### Example C — TypeScript

**Reviewer comment:** `retryCount reset to 0 right after increment; those two lines cancel out.`

**Before:**

```typescript
async function fetchWithRetry(url: string, maxRetries: number): Promise<Response> {
    let retryCount = 0;
    while (retryCount < maxRetries) {
        try {
            return await fetch(url);
        } catch {
            retryCount += 1;
            retryCount = 0;   // dead: resets what was just incremented
        }
    }
    throw new Error(`Failed after ${maxRetries} attempts`);
}
```

**After:**

```typescript
async function fetchWithRetry(url: string, maxRetries: number): Promise<Response> {
    let retryCount = 0;
    while (retryCount < maxRetries) {
        try {
            return await fetch(url);
        } catch {
            retryCount += 1;
        }
    }
    throw new Error(`Failed after ${maxRetries} attempts`);
}
```

---

## `use_framework_idiom` (inline-eligible)

The diff reimplements something the language's standard library or the surrounding runtime already ships as a single expression. The hand-rolled version adds lines, introduces edge-case risk, and signals unfamiliarity with the platform. The fix is to swap in the canonical one-liner.

### Example A — Python

**Reviewer comment:** `os.path.join already handles separator; no need to manually concatenate.`

**Before:**

```python
def config_path(base_dir: str, env: str, filename: str) -> str:
    sep = "/"
    if base_dir.endswith("/"):
        path = base_dir + env + sep + filename
    else:
        path = base_dir + sep + env + sep + filename
    return path
```

**After:**

```python
import os


def config_path(base_dir: str, env: str, filename: str) -> str:
    return os.path.join(base_dir, env, filename)
```

### Example B — Go

**Reviewer comment:** `time.Now().Unix() gives you a Unix timestamp; no arithmetic needed.`

**Before:**

```go
import (
    "time"
)

func currentUnixTimestamp() int64 {
    t := time.Now()
    epoch := time.Date(1970, 1, 1, 0, 0, 0, 0, time.UTC)
    return int64(t.Sub(epoch).Seconds())
}
```

**After:**

```go
import (
    "time"
)

func currentUnixTimestamp() int64 {
    return time.Now().Unix()
}
```

### Example C — TypeScript

**Reviewer comment:** `Object.fromEntries(map) does this in one call; drop the loop.`

**Before:**

```typescript
function mapToObject(m: Map<string, number>): Record<string, number> {
    const obj: Record<string, number> = {};
    m.forEach((value, key) => {
        obj[key] = value;
    });
    return obj;
}
```

**After:**

```typescript
function mapToObject(m: Map<string, number>): Record<string, number> {
    return Object.fromEntries(m);
}
```

---

## `rename` (discussion-only)

A name in the diff actively misleads the reader: it implies a type, scope, or concept that does not match what the identifier actually holds. The fix is a targeted rename — no logic changes. The bar for this archetype is a name that would send a future reader in the wrong direction, not just one that could be marginally clearer.

### Example A — Python

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

### Example B — Go

**Reviewer comment:** `tmp holds the final result; name it result so callers aren't confused.`

**Before:**

```go
func MergeConfigs(base, override Config) Config {
    tmp := base
    if override.Timeout > 0 {
        tmp.Timeout = override.Timeout
    }
    if override.MaxRetries > 0 {
        tmp.MaxRetries = override.MaxRetries
    }
    return tmp
}
```

**After:**

```go
func MergeConfigs(base, override Config) Config {
    result := base
    if override.Timeout > 0 {
        result.Timeout = override.Timeout
    }
    if override.MaxRetries > 0 {
        result.MaxRetries = override.MaxRetries
    }
    return result
}
```

### Example C — TypeScript

**Reviewer comment:** `oldData holds the *new* payload after the fetch; rename to freshData or newData.`

**Before:**

```typescript
async function refreshDashboard(boardId: string): Promise<void> {
    const oldData = await api.getDashboard(boardId);
    store.dispatch({ type: "DASHBOARD_LOADED", payload: oldData });
    renderWidgets(oldData.widgets);
}
```

**After:**

```typescript
async function refreshDashboard(boardId: string): Promise<void> {
    const freshData = await api.getDashboard(boardId);
    store.dispatch({ type: "DASHBOARD_LOADED", payload: freshData });
    renderWidgets(freshData.widgets);
}
```

---

## `replace_with_constant` (inline-eligible)

A raw literal — number, string, or arithmetic expression — is used inline instead of a named constant. The smell is either repetition (the same value appears in two or more places) or opacity (the value has a non-obvious domain meaning, like `86400` for seconds-in-a-day). The fix is a one-line extraction to a named constant at module or package scope, after which every use site becomes self-documenting and the value has a single source of truth.

### Example A — Python

**Reviewer comment:** `Magic number — extract to a named constant.`

**Before:**

```python
def is_session_expired(last_active: datetime) -> bool:
    delta = datetime.utcnow() - last_active
    return delta.total_seconds() > 86400


def expire_old_sessions(sessions: list[Session]) -> None:
    for session in sessions:
        if (datetime.utcnow() - session.last_active).total_seconds() > 86400:
            session.invalidate()
```

**After:**

```python
SESSION_EXPIRY_SECONDS = 86400  # 24 hours


def is_session_expired(last_active: datetime) -> bool:
    delta = datetime.utcnow() - last_active
    return delta.total_seconds() > SESSION_EXPIRY_SECONDS


def expire_old_sessions(sessions: list[Session]) -> None:
    for session in sessions:
        if (datetime.utcnow() - session.last_active).total_seconds() > SESSION_EXPIRY_SECONDS:
            session.invalidate()
```

### Example B — Go

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

### Example C — TypeScript

**Reviewer comment:** `Unexplained arithmetic — name this constant.`

**Before:**

```typescript
function scheduleRetry(attempt: number): number {
    return Math.min(attempt * 1000 * 60 * 60 * 24, 1000 * 60 * 60 * 24 * 7);
}

function isRetryExhausted(attempt: number): boolean {
    return attempt * 1000 * 60 * 60 * 24 > 1000 * 60 * 60 * 24 * 30;
}
```

**After:**

```typescript
const MS_PER_DAY = 1000 * 60 * 60 * 24;
const MAX_RETRY_WINDOW_MS = MS_PER_DAY * 7;
const RETRY_EXPIRY_MS = MS_PER_DAY * 30;

function scheduleRetry(attempt: number): number {
    return Math.min(attempt * MS_PER_DAY, MAX_RETRY_WINDOW_MS);
}

function isRetryExhausted(attempt: number): boolean {
    return attempt * MS_PER_DAY > RETRY_EXPIRY_MS;
}
```

---

## `swap_library_call` (inline-eligible)

A call to a standard-library or well-known library function that is technically functional but is the wrong API for the job: it may block when an async alternative exists, mutate when a copy-producing variant is available, use a deprecated entry point, or ignore locale/timezone context that the correct call handles automatically. The fix is a one-line substitution — no restructuring needed.

### Example A — Python

**Reviewer comment:** `Use \`datetime.now(timezone.utc)\` — \`utcnow()\` is deprecated and returns a naive datetime.`

**Before:**

```python
from datetime import datetime


def record_event(name: str) -> dict:
    return {"event": name, "recorded_at": datetime.utcnow().isoformat()}
```

**After:**

```python
from datetime import datetime, timezone


def record_event(name: str) -> dict:
    return {"event": name, "recorded_at": datetime.now(timezone.utc).isoformat()}
```

### Example B — Go

**Reviewer comment:** `\`ioutil.ReadAll\` is deprecated since Go 1.16 — use \`io.ReadAll\`.`

**Before:**

```go
import (
    "io/ioutil"
    "net/http"
)

func fetchBody(resp *http.Response) ([]byte, error) {
    defer resp.Body.Close()
    data, err := ioutil.ReadAll(resp.Body)
    if err != nil {
        return nil, err
    }
    return data, nil
}
```

**After:**

```go
import (
    "io"
    "net/http"
)

func fetchBody(resp *http.Response) ([]byte, error) {
    defer resp.Body.Close()
    data, err := io.ReadAll(resp.Body)
    if err != nil {
        return nil, err
    }
    return data, nil
}
```

### Example C — TypeScript

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

## `move_to_other_module` (discussion-only)

Code placed in the wrong architectural layer: a template or view doing data transformation that belongs in the model/service layer, a route handler encoding business rules that belong in a domain object, or an entry-point module accumulating logic that belongs in a dedicated module. Because the fix requires creating or extending a second file and updating the call site, this finding cannot be expressed as a single inline suggestion — it belongs in the review summary.

### Example A — Python

**Reviewer comment:** `Pricing logic in the view — move to the \`Order\` model or a service function.`

**Before** (`views.py`):

```python
def order_summary(request, order_id):
    order = Order.objects.get(pk=order_id)
    subtotal = sum(item.unit_price * item.quantity for item in order.items.all())
    discount = subtotal * Decimal("0.10") if order.customer.is_vip else Decimal("0")
    tax = (subtotal - discount) * Decimal("0.08")
    total = subtotal - discount + tax
    return render(request, "order_summary.html", {"order": order, "total": total})
```

**After** (`models.py` — new method):

```python
class Order(models.Model):
    ...

    def calculate_total(self) -> Decimal:
        subtotal = sum(item.unit_price * item.quantity for item in self.items.all())
        discount = subtotal * Decimal("0.10") if self.customer.is_vip else Decimal("0")
        tax = (subtotal - discount) * Decimal("0.08")
        return subtotal - discount + tax
```

**After** (`views.py` — simplified call site):

```python
def order_summary(request, order_id):
    order = Order.objects.get(pk=order_id)
    return render(request, "order_summary.html", {"order": order, "total": order.calculate_total()})
```

### Example B — Go

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

### Example C — TypeScript

**Reviewer comment:** `Slug generation is domain logic — move it out of the API route handler.`

**Before** (`routes/articles.ts`):

```typescript
router.post("/articles", async (req, res) => {
    const { title, body, authorId } = req.body;

    const slug = title
        .toLowerCase()
        .trim()
        .replace(/[^\w\s-]/g, "")
        .replace(/\s+/g, "-")
        .replace(/-+/g, "-");

    const article = await db.articles.create({ title, slug, body, authorId });
    res.status(201).json(article);
});
```

**After** (`domain/article.ts` — new utility):

```typescript
export function slugify(title: string): string {
    return title
        .toLowerCase()
        .trim()
        .replace(/[^\w\s-]/g, "")
        .replace(/\s+/g, "-")
        .replace(/-+/g, "-");
}
```

**After** (`routes/articles.ts` — simplified call site):

```typescript
import { slugify } from "../domain/article";

router.post("/articles", async (req, res) => {
    const { title, body, authorId } = req.body;
    const article = await db.articles.create({ title, slug: slugify(title), body, authorId });
    res.status(201).json(article);
});
```

---

## `extract_to_helper` (discussion-only)

This archetype appears when the same multi-step operation is copy-pasted two or more times across a file or module. The duplication is structural: the same sequence of steps, usually parameterised only by a small set of values, repeats verbatim. The fix is to give that sequence a name and collapse each call site to one line. Future call sites then get the named helper for free, and any change to the logic happens in one place.

### Example A — Python

**Reviewer comment:** `build_error_response` is copied verbatim three times. Extract it to a helper and call it from each handler.

**Before:**

```python
def handle_not_found(resource_id: str) -> dict:
    return {
        "ok": False,
        "error": {
            "code": "NOT_FOUND",
            "detail": f"Resource {resource_id!r} was not found.",
            "timestamp": datetime.utcnow().isoformat(),
        },
    }


def handle_forbidden(resource_id: str) -> dict:
    return {
        "ok": False,
        "error": {
            "code": "FORBIDDEN",
            "detail": f"Access to {resource_id!r} is denied.",
            "timestamp": datetime.utcnow().isoformat(),
        },
    }
```

**After:**

```python
def _error_response(code: str, detail: str) -> dict:
    return {"ok": False, "error": {"code": code, "detail": detail, "timestamp": datetime.utcnow().isoformat()}}


def handle_not_found(resource_id: str) -> dict:
    return _error_response("NOT_FOUND", f"Resource {resource_id!r} was not found.")


def handle_forbidden(resource_id: str) -> dict:
    return _error_response("FORBIDDEN", f"Access to {resource_id!r} is denied.")
```

---

### Example B — Go

**Reviewer comment:** The pagination slice + total-count pattern is duplicated across every list handler. Pull it into a helper so callers don't diverge if the logic changes.

**Before:**

```go
func listUsers(items []User, page, size int) Page[User] {
    start := (page - 1) * size
    end := start + size
    if end > len(items) {
        end = len(items)
    }
    return Page[User]{Items: items[start:end], Total: len(items)}
}

func listOrders(items []Order, page, size int) Page[Order] {
    start := (page - 1) * size
    end := start + size
    if end > len(items) {
        end = len(items)
    }
    return Page[Order]{Items: items[start:end], Total: len(items)}
}
```

**After:**

```go
func paginate[T any](items []T, page, size int) Page[T] {
    start := (page - 1) * size
    end := start + size
    if end > len(items) {
        end = len(items)
    }
    return Page[T]{Items: items[start:end], Total: len(items)}
}

func listUsers(items []User, page, size int) Page[User] {
    return paginate(items, page, size)
}

func listOrders(items []Order, page, size int) Page[Order] {
    return paginate(items, page, size)
}
```

---

### Example C — TypeScript

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

### Example A — Python

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

### Example B — Go

**Reviewer comment:** `ID`, `CreatedAt`, and `UpdatedAt` are copy-pasted into every entity struct. Embed a `BaseEntity` so new structs inherit them automatically and JSON tags stay consistent.

**Before:**

```go
type Product struct {
    ID        int       `json:"id" db:"id"`
    CreatedAt time.Time `json:"created_at" db:"created_at"`
    UpdatedAt time.Time `json:"updated_at" db:"updated_at"`
    Name      string    `json:"name" db:"name"`
    Price     float64   `json:"price" db:"price"`
}

type Warehouse struct {
    ID        int       `json:"id" db:"id"`
    CreatedAt time.Time `json:"created_at" db:"created_at"`
    UpdatedAt time.Time `json:"updated_at" db:"updated_at"`
    Location  string    `json:"location" db:"location"`
    Capacity  int       `json:"capacity" db:"capacity"`
}
```

**After:**

```go
type BaseEntity struct {
    ID        int       `json:"id" db:"id"`
    CreatedAt time.Time `json:"created_at" db:"created_at"`
    UpdatedAt time.Time `json:"updated_at" db:"updated_at"`
}

type Product struct {
    BaseEntity
    Name  string  `json:"name" db:"name"`
    Price float64 `json:"price" db:"price"`
}

type Warehouse struct {
    BaseEntity
    Location string `json:"location" db:"location"`
    Capacity int    `json:"capacity" db:"capacity"`
}
```

---

### Example C — TypeScript

**Reviewer comment:** Both resource classes repeat the same `id`/`createdAt`/`serialize` contract. Extract a `BaseResource` class so the shared shape is defined once and sub-classes only add what's unique to them.

**Before:**

```typescript
class Invoice {
    id: string;
    createdAt: Date;
    amount: number;

    constructor(id: string, amount: number) {
        this.id = id;
        this.createdAt = new Date();
        this.amount = amount;
    }

    serialize() {
        return { id: this.id, createdAt: this.createdAt.toISOString(), amount: this.amount };
    }
}

class Receipt {
    id: string;
    createdAt: Date;
    total: number;

    constructor(id: string, total: number) {
        this.id = id;
        this.createdAt = new Date();
        this.total = total;
    }

    serialize() {
        return { id: this.id, createdAt: this.createdAt.toISOString(), total: this.total };
    }
}
```

**After:**

```typescript
abstract class BaseResource {
    id: string;
    createdAt: Date;

    constructor(id: string) {
        this.id = id;
        this.createdAt = new Date();
    }

    protected baseFields() {
        return { id: this.id, createdAt: this.createdAt.toISOString() };
    }
}

class Invoice extends BaseResource {
    constructor(id: string, public amount: number) { super(id); }
    serialize() { return { ...this.baseFields(), amount: this.amount }; }
}

class Receipt extends BaseResource {
    constructor(id: string, public total: number) { super(id); }
    serialize() { return { ...this.baseFields(), total: this.total }; }
}
```

---

## `split_responsibility` (discussion-only)

This archetype appears when a single function, class, or module is doing two clearly orthogonal jobs — for example, fetching data and transforming it, or validating input and writing audit logs. The tell is that the unit has two reasons to change: once for each responsibility. Reviewers often spot it when the function name uses "and", when a class has method groups with no shared state, or when a unit is hard to test because mocking one concern forces you to stub the other. The fix is to separate the two responsibilities into distinct units and compose them at the call site.

### Example A — Python

**Reviewer comment:** `import_csv` fetches, parses, and persists in one function — three separate reasons to change. Split into `read_csv_rows`, `parse_rows`, and `save_records` and compose them in the caller.

**Before:**

```python
def import_csv(path: str, session: Session) -> int:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    records = []
    for row in rows:
        records.append(
            Record(name=row["name"].strip(), value=Decimal(row["value"]), active=row["active"].lower() == "true")
        )

    session.bulk_save_objects(records)
    session.commit()
    return len(records)
```

**After:**

```python
def read_csv_rows(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def parse_rows(rows: list[dict]) -> list[Record]:
    return [
        Record(name=row["name"].strip(), value=Decimal(row["value"]), active=row["active"].lower() == "true")
        for row in rows
    ]


def save_records(records: list[Record], session: Session) -> int:
    session.bulk_save_objects(records)
    session.commit()
    return len(records)


# caller
def import_csv(path: str, session: Session) -> int:
    return save_records(parse_rows(read_csv_rows(path)), session)
```

---

### Example B — Go

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

### Example C — TypeScript

**Reviewer comment:** `syncUserProfile` is fetching from the remote API and normalising the result in one go. These are independent concerns — split them so the normaliser can be unit-tested without a network stub.

**Before:**

```typescript
async function syncUserProfile(userId: string): Promise<UserProfile> {
    const resp = await fetch(`/api/users/${userId}`);
    if (!resp.ok) throw new Error(`Fetch failed: ${resp.status}`);
    const raw = await resp.json();

    return {
        id: raw.user_id,
        displayName: `${raw.first_name} ${raw.last_name}`.trim(),
        email: raw.email_address.toLowerCase(),
        avatarUrl: raw.profile_picture ?? null,
        isActive: raw.status === "active",
    };
}
```

**After:**

```typescript
async function fetchRawUser(userId: string): Promise<unknown> {
    const resp = await fetch(`/api/users/${userId}`);
    if (!resp.ok) throw new Error(`Fetch failed: ${resp.status}`);
    return resp.json();
}

function normaliseUser(raw: Record<string, unknown>): UserProfile {
    return {
        id: raw.user_id as string,
        displayName: `${raw.first_name} ${raw.last_name}`.trim(),
        email: (raw.email_address as string).toLowerCase(),
        avatarUrl: (raw.profile_picture as string) ?? null,
        isActive: raw.status === "active",
    };
}

// caller
async function syncUserProfile(userId: string): Promise<UserProfile> {
    return normaliseUser(await fetchRawUser(userId) as Record<string, unknown>);
}
```

---

## `add_guard` (discussion-only)

This archetype appears whenever code reaches an operation that requires a precondition — unwrapping an optional, using a value from external input, dereferencing a pointer — without first verifying that precondition holds. The trust boundary is the key: data arriving from outside the current module (HTTP request, database row, user argument, network response) can be absent, malformed, or out of range. The fix is a small early-return or error-return that rejects the bad input before it propagates deeper.

### Example A — Python

**Reviewer comment:** `user_id` comes from the request — it can be missing or non-numeric. Validate before the DB query, not after.

**Before:**

```python
def get_user_profile(request):
    user_id = request.GET.get("user_id")
    user = db.session.query(User).filter_by(id=int(user_id)).first()
    if user is None:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse(user.to_dict())
```

**After:**

```python
def get_user_profile(request):
    user_id = request.GET.get("user_id")
    if not user_id or not user_id.isdigit():
        return JsonResponse({"error": "user_id must be a positive integer"}, status=400)
    user = db.session.query(User).filter_by(id=int(user_id)).first()
    if user is None:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse(user.to_dict())
```

### Example B — Go

**Reviewer comment:** `limit` is caller-supplied; a zero or negative value will silently return nothing. Guard the boundary.

**Before:**

```go
func ListItems(ctx context.Context, limit int) ([]Item, error) {
    rows, err := db.QueryContext(ctx,
        "SELECT id, name FROM items ORDER BY created_at DESC LIMIT $1", limit)
    if err != nil {
        return nil, err
    }
    defer rows.Close()
    var items []Item
    for rows.Next() {
        var item Item
        if err := rows.Scan(&item.ID, &item.Name); err != nil {
            return nil, err
        }
        items = append(items, item)
    }
    return items, rows.Err()
}
```

**After:**

```go
func ListItems(ctx context.Context, limit int) ([]Item, error) {
    if limit <= 0 {
        return nil, fmt.Errorf("limit must be a positive integer, got %d", limit)
    }
    if limit > 1000 {
        return nil, fmt.Errorf("limit exceeds maximum of 1000, got %d", limit)
    }
    rows, err := db.QueryContext(ctx,
        "SELECT id, name FROM items ORDER BY created_at DESC LIMIT $1", limit)
    if err != nil {
        return nil, err
    }
    defer rows.Close()
    var items []Item
    for rows.Next() {
        var item Item
        if err := rows.Scan(&item.ID, &item.Name); err != nil {
            return nil, err
        }
        items = append(items, item)
    }
    return items, rows.Err()
}
```

### Example C — TypeScript

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

### Example A — Python

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

### Example B — Go

**Reviewer comment:** `Role_UNKNOWN` can't appear here — it means the role map was built incorrectly. Panic rather than silently returning an empty set.

**Before:**

```go
func permissionsForRole(role Role) []string {
    switch role {
    case Role_ADMIN:
        return []string{"read", "write", "delete"}
    case Role_EDITOR:
        return []string{"read", "write"}
    case Role_VIEWER:
        return []string{"read"}
    default:
        // shouldn't happen
        return []string{}
    }
}
```

**After:**

```go
func permissionsForRole(role Role) []string {
    switch role {
    case Role_ADMIN:
        return []string{"read", "write", "delete"}
    case Role_EDITOR:
        return []string{"read", "write"}
    case Role_VIEWER:
        return []string{"read"}
    default:
        panic(fmt.Sprintf("permissionsForRole: unhandled role %v — add a case or fix the caller", role))
    }
}
```

### Example C — TypeScript

**Reviewer comment:** `order.completedAt` being `null` here means the caller skipped the status check. Don't swallow it — throw to surface the contract violation.

**Before:**

```typescript
function buildReceipt(order: Order): Receipt {
    // Called only after order.status === "completed" is confirmed by the router
    if (order.completedAt === null) {
        // shouldn't reach here
        return { orderId: order.id, completedAt: new Date(0), total: order.total };
    }
    return {
        orderId: order.id,
        completedAt: order.completedAt,
        total: order.total,
    };
}
```

**After:**

```typescript
function buildReceipt(order: Order): Receipt {
    // Called only after order.status === "completed" is confirmed by the router
    if (order.completedAt === null) {
        throw new Error(
            `Invariant violated: buildReceipt called with order ${order.id} where completedAt is null`
        );
    }
    return {
        orderId: order.id,
        completedAt: order.completedAt,
        total: order.total,
    };
}
```

---

## `fix_logic_bug` (discussion-only)

This archetype covers code that computes the wrong result on ordinary inputs: an off-by-one that skips the last element, an inverted condition that rejects valid input instead of invalid, a `=` that resets an accumulator instead of `+=` that builds it, or an index that reads from the wrong position in a loop. These bugs are usually subtle — the code looks plausible at a glance — but they produce consistently wrong outputs that tests or careful review expose. The after must show the correct logic, not a comment or a TODO.

### Example A — Python

**Reviewer comment:** The slice `pages[1:page_count]` skips `pages[0]` and misses `pages[page_count - 1]`. Should be `pages[0:page_count]` (or just `pages[:page_count]`).

**Before:**

```python
def extract_text(pages: list[str], page_count: int) -> str:
    """Return concatenated text for the first page_count pages."""
    selected = pages[1:page_count]
    return "\n".join(selected)
```

**After:**

```python
def extract_text(pages: list[str], page_count: int) -> str:
    """Return concatenated text for the first page_count pages."""
    selected = pages[:page_count]
    return "\n".join(selected)
```

### Example B — Go

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

### Example C — TypeScript

**Reviewer comment:** `results[results.length]` is always `undefined` — array indices end at `length - 1`. Should be `results[results.length - 1]`.

**Before:**

```typescript
async function pollUntilDone(jobId: string): Promise<JobResult> {
    const results: JobResult[] = [];
    for (let attempt = 0; attempt < 10; attempt++) {
        const result = await fetchJobStatus(jobId);
        results.push(result);
        if (result.status === "done" || result.status === "failed") {
            break;
        }
        await delay(1000);
    }
    const last = results[results.length];
    if (!last) {
        throw new Error("job did not complete within the retry window");
    }
    return last;
}
```

**After:**

```typescript
async function pollUntilDone(jobId: string): Promise<JobResult> {
    const results: JobResult[] = [];
    for (let attempt = 0; attempt < 10; attempt++) {
        const result = await fetchJobStatus(jobId);
        results.push(result);
        if (result.status === "done" || result.status === "failed") {
            break;
        }
        await delay(1000);
    }
    const last = results[results.length - 1];
    if (!last) {
        throw new Error("job did not complete within the retry window");
    }
    return last;
}
```
