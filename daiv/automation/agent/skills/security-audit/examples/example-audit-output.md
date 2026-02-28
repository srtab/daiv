# Example Security Audit Output

This is a reference example of a well-formed audit report. Use this as a format anchor when writing real audit output — match structure, depth per finding, and tone.

---

## Security Audit: PR #247 — "Add user profile export feature"

**Scope:** `src/controllers/profile.py`, `src/routes/export.py`, `src/utils/file_handler.py`
**Diff:** 3 files changed, +187 / -12 lines
**Audited:** 2024-11-15

---

### Summary

- **Overall posture:** High risk. This PR introduces two exploitable vulnerabilities that should block merge.
- **Findings:** 1 Critical (C-01), 1 High (H-01), 2 Medium (M-01, M-02), 1 Low (L-01).
- **Hotspot:** `file_handler.py` — C-01 combines path traversal with an unescaped shell command into a full read/write exploit chain.

---

### Findings

#### 🔴 C-01 — Path Traversal + Command Injection in Export Handler

**Location:** `src/utils/file_handler.py`, `generate_export()`, line 34
**Risk:** An authenticated user can read arbitrary files from the server filesystem (e.g., `/etc/passwd`, application secrets, other users' exports). If chained with the shell command on line 41, they can achieve remote code execution.

**Evidence:**
```python
# line 34 — filename comes from user-supplied profile_name
export_path = f"/tmp/exports/{user_id}/{profile_name}.csv"

# line 41 — unescaped shell command using the same filename
os.system(f"zip /tmp/exports/{user_id}.zip {export_path}")
```

A request with `profile_name=../../../../etc/passwd%00` would set `export_path` to `/tmp/exports/42/../../../../etc/passwd` and pass it to `os.system`, which resolves the traversal and includes the file in the zip — or executes arbitrary commands if further crafted.

**Remediation:**
```python
import os, subprocess
from pathlib import Path

EXPORT_BASE = Path("/tmp/exports").resolve()

def generate_export(user_id: int, profile_name: str) -> Path:
    # Sanitize filename: allow only alphanumeric, dash, underscore
    safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", profile_name)
    export_path = (EXPORT_BASE / str(user_id) / f"{safe_name}.csv").resolve()

    # Ensure path stays within the allowed base directory
    if not str(export_path).startswith(str(EXPORT_BASE)):
        raise ValueError("Invalid export path")

    # Use subprocess list form — no shell interpolation
    subprocess.run(
        ["zip", f"/tmp/exports/{user_id}.zip", str(export_path)],
        check=True
    )
    return export_path
```

---

#### 🔴 H-01 — Missing Authorization Check on Export Download Endpoint

**Location:** `src/routes/export.py`, `download_export()`, line 19
**Risk:** Any authenticated user can download any other user's export file by guessing or enumerating export IDs. This is an insecure direct object reference (IDOR).

**Evidence:**
```python
@app.route("/export/<int:export_id>/download")
@login_required
def download_export(export_id):
    export = Export.query.get_or_404(export_id)
    return send_file(export.path)  # no ownership check
```

**Remediation:**
```python
@app.route("/export/<int:export_id>/download")
@login_required
def download_export(export_id):
    export = Export.query.filter_by(
        id=export_id,
        user_id=current_user.id  # enforce ownership
    ).first_or_404()
    return send_file(export.path)
```

---

#### 🟡 M-01 — Stack Trace Exposed in Export Error Response

**Location:** `src/controllers/profile.py`, `export_profile()`, line 67
**Risk:** If export generation fails, the full Python stack trace including internal file paths and library versions is returned to the client. This aids attacker reconnaissance.

**Evidence:**
```python
except Exception as e:
    return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500
```

**Remediation:** Log the full trace server-side; return only a generic error message to the client.

```python
except Exception as e:
    logger.error("Export failed for user %s", current_user.id, exc_info=True)
    return jsonify({"error": "Export failed. Please try again."}), 500
```

---

#### 🟡 M-02 — Export Directory Created with World-Readable Permissions

**Location:** `src/utils/file_handler.py`, `setup_export_dir()`, line 12
**Risk:** Export files containing user PII are written to a directory with `0o777` permissions, allowing any process on the host to read them. In a shared hosting or container-escape scenario, this leaks user data.

**Evidence:**
```python
os.makedirs(export_dir, mode=0o777, exist_ok=True)
```

**Remediation:** Use `0o700` (owner read/write/execute only).

---

#### 🔵 L-01 — No Rate Limiting on Export Endpoint

**Location:** `src/routes/export.py`
**Risk:** A user could trigger export generation in a loop, causing CPU/disk exhaustion. Not directly exploitable but a denial-of-service risk at scale.

**Remediation:** Add a rate limit (e.g., max 10 exports per user per hour) using Flask-Limiter or equivalent.

---

### Recommendations

- **Add integration test for IDOR:** The download endpoint had no ownership check and no test covering cross-user access. Add a test where user A attempts to download user B's export and assert 403.
- **Implement a secrets scanner in CI:** The hardcoded export path prefix `/tmp/exports` isn't a secret, but the pattern of using `os.system` with string interpolation would be caught by a linter like `bandit`. Consider adding `bandit` to the CI pipeline.
- **Review all other file-handling utilities:** `file_handler.py` appears to be a general utility. Audit other callers of `generate_export()` for the same path traversal pattern.

---

### Testing & Validation

- [ ] Test path traversal: send `profile_name=../../../../etc/passwd` and verify the server returns 400, not a zip file.
- [ ] Test IDOR: create two test users, generate an export as user A, attempt download as user B — assert 403/404.
- [ ] Test error handling: trigger a deliberate export failure, assert the response body contains no stack trace.
- [ ] Run `bandit -r src/` and address any high-severity findings before merge.
