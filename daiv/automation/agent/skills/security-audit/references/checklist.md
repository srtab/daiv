# Security Audit Checklist — Detection Heuristics

Reference this file during the audit phase for specific signals to look for in each category. These are ordered roughly by frequency of real-world findings.

---

## Authentication & Authorization

**What to look for:**
- Missing auth checks before accessing sensitive resources (routes with no middleware, functions that skip permission validation)
- Broken access control: user A can read/modify user B's data by changing an ID in the request
- Privilege escalation: user-supplied role or permission values accepted without server-side validation
- JWT: `alg: none` accepted, weak secrets (`secret`, `password`), tokens not validated on every request, missing expiry check
- Session: session fixation, session IDs in URLs, long-lived tokens without rotation
- Password reset flows: predictable tokens, no expiry, token reuse allowed

**High-signal code patterns:**
```python
# Dangerous: user ID taken from request, not from session
user_id = request.params["user_id"]

# Dangerous: role from JWT payload without server-side check
if token_payload["role"] == "admin":
    ...


# Dangerous: no auth on sensitive route
@app.route("/admin/delete", methods=["POST"])
def delete_user(): ...
```

---

## Input Validation & Injection

### SQL Injection
**What to look for:** String concatenation or f-strings used to build queries; `.raw()`, `.execute()`, or `cursor.execute()` with user input; ORM escape bypasses.

```python
# Dangerous
query = f"SELECT * FROM users WHERE name = '{name}'"
cursor.execute("DELETE FROM orders WHERE id = " + order_id)

# Safe
cursor.execute("SELECT * FROM users WHERE name = %s", (name,))
```

### XSS
**What to look for:** User input rendered unescaped into HTML; `dangerouslySetInnerHTML` in React; `.innerHTML =` assignments; template engines with unescaped interpolation (`{{{ }}}` in Handlebars, `| safe` in Jinja2).

### Command Injection
**What to look for:** `subprocess`, `exec`, `eval`, `os.system`, `shell=True` with any user-controlled input; `child_process.exec` in Node with string interpolation.

```python
# Dangerous
os.system(f"convert {filename} output.png")  # filename is user input

# Safe
subprocess.run(["convert", filename, "output.png"])
```

### SSRF (Server-Side Request Forgery)
**What to look for:** Server making HTTP requests to URLs supplied by the user; webhook URLs, avatar URLs, import-from-URL features with no allowlist.

### Path Traversal
**What to look for:** File paths constructed from user input without canonicalization; `../` sequences not stripped; `os.path.join` used without checking the result stays within an allowed directory.

---

## Secrets Management

**What to look for:**
- Hardcoded strings matching known secret patterns: API keys, tokens, passwords, connection strings in source code
- Secrets in log statements (`logger.info(f"Connecting with password {pw}")`)
- Secrets returned in API responses or error messages
- `.env` files or config files committed to the repo
- Environment variables exposed to client-side code (e.g., Next.js `NEXT_PUBLIC_*` prefix used for secrets)

**Patterns to scan for:**
```
password = "..."
api_key = "..."
SECRET_KEY = "..."
Authorization: "Bearer sk-..."
mongodb+srv://user:password@...
```

---

## Data Protection

**What to look for:**
- PII (emails, SSNs, phone numbers, addresses) stored without encryption or hashing
- Passwords stored as plaintext or with weak hashing (MD5, SHA1 without salt)
- Sensitive data transmitted over HTTP (not HTTPS)
- Sensitive data included in logs, analytics events, or error tracking
- Over-fetching: queries returning more columns/rows than the feature needs
- Missing `HttpOnly` and `Secure` flags on session cookies

---

## Dependency & Supply-Chain Risks

**What to look for:**
- Packages with known CVEs (check package versions against NVD or Snyk)
- Unpinned dependency versions (`*`, `latest`, `^` ranges that allow major bumps)
- Dependencies with very few downloads or recent ownership changes (potential typosquatting)
- Direct use of GitHub URLs or git SHAs as dependencies without verification
- `postinstall` scripts in `package.json` that run arbitrary code

---

## Error Handling

**What to look for:**
- Stack traces or internal paths returned to the client in error responses
- Database error messages exposed to users (may reveal schema, query structure)
- Catch-all exception handlers that swallow errors silently (makes detection of attacks harder)
- Different error messages for "user not found" vs "wrong password" (username enumeration)

```python
# Dangerous: exposes internals
except Exception as e:
    return jsonify({"error": str(e)}), 500

# Safe
except Exception as e:
    logger.error("Internal error", exc_info=True)
    return jsonify({"error": "An unexpected error occurred"}), 500
```

---

## Cryptography

**What to look for:**
- Weak or broken algorithms: MD5, SHA1, DES, RC4, ECB mode for block ciphers
- Hardcoded IVs or nonces (must be random per operation)
- `random` module used for security purposes (use `secrets` or `os.urandom` instead)
- RSA without OAEP padding; ECDSA with reused k values
- TLS: accepting SSLv3, TLS 1.0/1.1; skipping certificate verification (`verify=False`)
- Encryption used where signing is needed (or vice versa)

---

## API Security

**What to look for:**
- Endpoints missing authentication or authorization middleware
- CORS: `Access-Control-Allow-Origin: *` on endpoints that handle credentials or sensitive data
- No rate limiting on auth endpoints (login, password reset, OTP verification)
- GraphQL: introspection enabled in production; no query depth/complexity limits
- Sensitive operations accessible via GET requests (should be POST/PUT/DELETE)
- Missing CSRF protection on state-changing endpoints that use cookie auth
