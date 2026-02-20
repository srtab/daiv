# Example: Complex Plan — Add JWT Authentication

**User request:** "We need to add authentication to the API. Users should be able to register, log in, and get a JWT that protects our existing endpoints. Plan it out."

**Complexity assessment:** Complex. Touches multiple layers (routes, middleware, DB, tests). Multi-day effort. Security-sensitive.

**Exploration performed:** Read `src/routes/`, `src/middleware/`, `src/models/`, `package.json`. Traced a full request lifecycle through `server.js` → router → controller → model. Checked git log for any prior auth attempts. Confirmed no existing session or auth middleware present.

---

# Implementation Plan: JWT Authentication

## 1. Overview

Add stateless JWT-based authentication to the Express API. Users register with email/password, receive a token on login, and include that token in subsequent requests. All existing `/api/*` endpoints will require a valid token. The implementation follows the existing controller/model pattern already used for `User`, `Post`, and `Comment` resources.

## 2. Current State Analysis

- Express app with route files in `src/routes/`, controllers in `src/controllers/`, Mongoose models in `src/models/`.
- No authentication middleware exists. All routes are currently public.
- `User` model exists (`src/models/User.js`) with `name` and `email` fields — no `password` field yet.
- Error handling uses a centralized `errorHandler` middleware in `src/middleware/errorHandler.js`. New auth errors should use the same pattern.
- Tests use Jest + Supertest. Auth routes will need integration tests following the pattern in `src/routes/__tests__/posts.test.js`.
- `jsonwebtoken` and `bcryptjs` are not yet installed.

## 3. Proposed Solution

Add a lightweight auth layer:
1. Extend `User` model with a hashed password field.
2. Add `/auth/register` and `/auth/login` routes that return a signed JWT.
3. Add `authenticate` middleware that validates the token and attaches `req.user`.
4. Apply the middleware to all existing `/api/*` routes via the router, not individually per route.

**Alternative considered:** Passport.js. Rejected — adds complexity and abstraction not warranted for this API's scope. Raw `jsonwebtoken` is sufficient and keeps the codebase consistent.

**Security decisions:** Tokens expire in 7 days. Passwords hashed with bcrypt, salt rounds = 10. JWT secret from `process.env.JWT_SECRET` — must not be hardcoded.

## 4. Implementation Phases

### Phase 1: Dependencies and Model
- Step 1.1: Install `jsonwebtoken` and `bcryptjs` — required by all subsequent steps
- Step 1.2: Extend `User` model — add `password` field with pre-save bcrypt hook and `comparePassword` instance method
- Step 1.3: Add `JWT_SECRET` and `JWT_EXPIRES_IN` to `.env.example` and document in README

### Phase 2: Auth Routes
- Step 2.1: Create `AuthController` with `register` and `login` methods
- Step 2.2: Create `src/routes/auth.js` and mount at `/auth` in `server.js`
- Step 2.3: Add input validation (required fields, email format, password min length 8)

### Phase 3: Middleware and Route Protection
- Step 3.1: Create `src/middleware/authenticate.js` — extract Bearer token, verify JWT, attach `req.user`, call `next()` or return 401
- Step 3.2: Apply `authenticate` middleware to the `/api` router in `server.js` (one line, protects all existing routes)

### Phase 4: Tests
- Step 4.1: Write integration tests for `/auth/register` (success, duplicate email, missing fields)
- Step 4.2: Write integration tests for `/auth/login` (success, wrong password, unknown email)
- Step 4.3: Write middleware unit test (valid token, expired token, missing token, malformed token)
- Step 4.4: Update existing route tests to include a valid auth token in request headers

## 5. File Changes

| File | Action | Purpose |
|------|--------|---------|
| `package.json` | Modify | Add `jsonwebtoken`, `bcryptjs` |
| `src/models/User.js` | Modify | Add password field, bcrypt hook, comparePassword method |
| `src/controllers/AuthController.js` | Create | Register and login logic |
| `src/routes/auth.js` | Create | Mount `/register` and `/login` |
| `src/middleware/authenticate.js` | Create | JWT verification middleware |
| `server.js` | Modify | Mount auth router, apply authenticate to `/api` |
| `.env.example` | Modify | Add JWT_SECRET, JWT_EXPIRES_IN |
| `src/routes/__tests__/auth.test.js` | Create | Auth route integration tests |
| `src/middleware/__tests__/authenticate.test.js` | Create | Middleware unit tests |
| `src/routes/__tests__/posts.test.js` | Modify | Add auth headers to existing tests |
| `src/routes/__tests__/comments.test.js` | Modify | Add auth headers to existing tests |

## 6. Dependencies and Configuration

```
npm install jsonwebtoken bcryptjs
```

New environment variables (add to `.env` and `.env.example`):
```
JWT_SECRET=<random 64-char string>
JWT_EXPIRES_IN=7d
```

## 7. Testing Strategy

**Integration tests** (Jest + Supertest): Full request lifecycle for register and login. Verify token is returned and usable. Verify protected routes return 401 without token.

**Middleware unit tests**: Test `authenticate.js` in isolation by passing mock `req`/`res`/`next`. Cover: valid token, expired token, wrong secret, missing Authorization header, malformed Bearer format.

**Existing tests:** Update all route tests to generate a test token in `beforeAll` and include it in headers. Use a fixed `JWT_SECRET=test-secret` in the test environment.

**Manual smoke test sequence:**
1. POST `/auth/register` → get token
2. GET `/api/posts` with token → 200
3. GET `/api/posts` without token → 401
4. POST `/auth/login` with wrong password → 401

## 8. Edge Cases and Error Handling

- Duplicate email on register → 409 Conflict with message "Email already in use"
- Missing required fields → 400 with field-level errors (follow existing `errorHandler` format)
- Wrong password → 401 "Invalid credentials" (do not distinguish wrong email vs wrong password — prevents user enumeration)
- Expired token → 401 "Token expired"
- Malformed token → 401 "Invalid token"
- `JWT_SECRET` not set in environment → throw at startup, not at request time

## 9. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `JWT_SECRET` accidentally committed | Low | High | Add to `.gitignore`, validate presence at startup |
| Existing tests break when routes go protected | High | Medium | Update all test files in Phase 4 before merging |
| bcrypt rounds too high, login is slow | Low | Low | Salt rounds = 10 is standard; benchmark if needed |
| Token not expiring in tests causes flaky results | Medium | Low | Use short expiry (`60s`) in test env via `JWT_EXPIRES_IN` env var |

## 10. Open Questions

- Should `/api/users/:id` (the profile endpoint) be public or protected? Currently assuming protected.
- Is there a token refresh requirement, or is re-login on expiry acceptable?
- Should failed login attempts be rate-limited? (Recommend yes, but out of scope for this plan unless confirmed.)
