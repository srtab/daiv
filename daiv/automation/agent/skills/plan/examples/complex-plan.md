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
1. **Install auth dependencies** — required by all subsequent steps
   - `package.json` — add `jsonwebtoken`, `bcryptjs`

2. **Extend User model** — add password field with pre-save bcrypt hook and `comparePassword` instance method
   - `src/models/User.js` (line ~8) — add `password` field, bcrypt hook, instance method

3. **Add environment config** — document required secrets
   - `.env.example` — add `JWT_SECRET`, `JWT_EXPIRES_IN`

### Phase 2: Auth Routes
1. **Create auth controller** — register and login logic with input validation
   - `src/controllers/AuthController.js` — Create — `register` and `login` methods
   - Validate: required fields, email format, password min length 8

2. **Mount auth routes** — expose `/auth/register` and `/auth/login`
   - `src/routes/auth.js` — Create — route definitions
   - `server.js` (line ~15) — mount auth router at `/auth`

### Phase 3: Middleware and Route Protection
1. **Create JWT verification middleware** — extract Bearer token, verify, attach `req.user`, call `next()` or return 401
   - `src/middleware/authenticate.js` — Create

2. **Protect existing routes** — apply middleware to all `/api` routes
   - `server.js` (line ~18) — one line, apply `authenticate` to `/api` router

### Phase 4: Tests
1. **Auth route integration tests** — register and login flows
   - `src/routes/__tests__/auth.test.js` — Create — success, duplicate email, missing fields, wrong password, unknown email

2. **Middleware unit tests** — verify token handling in isolation
   - `src/middleware/__tests__/authenticate.test.js` — Create — valid token, expired token, missing token, malformed token

3. **Update existing tests** — add auth headers so they pass under protection
   - `src/routes/__tests__/posts.test.js` — add token in `beforeAll`
   - `src/routes/__tests__/comments.test.js` — add token in `beforeAll`

## 5. Dependencies and Configuration

```
npm install jsonwebtoken bcryptjs
```

New environment variables (add to `.env` and `.env.example`):
```
JWT_SECRET=<random 64-char string>
JWT_EXPIRES_IN=7d
```

## 6. Testing Strategy

**Integration tests** (Jest + Supertest): Full request lifecycle for register and login. Verify token is returned and usable. Verify protected routes return 401 without token.

**Middleware unit tests**: Test `authenticate.js` in isolation by passing mock `req`/`res`/`next`. Cover: valid token, expired token, wrong secret, missing Authorization header, malformed Bearer format.

**Existing tests:** Update all route tests to generate a test token in `beforeAll` and include it in headers. Use a fixed `JWT_SECRET=test-secret` in the test environment.

**Manual smoke test sequence:**
1. POST `/auth/register` → get token
2. GET `/api/posts` with token → 200
3. GET `/api/posts` without token → 401
4. POST `/auth/login` with wrong password → 401

## 7. Edge Cases and Error Handling

- Duplicate email on register → 409 Conflict with message "Email already in use"
- Missing required fields → 400 with field-level errors (follow existing `errorHandler` format)
- Wrong password → 401 "Invalid credentials" (do not distinguish wrong email vs wrong password — prevents user enumeration)
- Expired token → 401 "Token expired"
- Malformed token → 401 "Invalid token"
- `JWT_SECRET` not set in environment → throw at startup, not at request time

## 8. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `JWT_SECRET` accidentally committed | Low | High | Add to `.gitignore`, validate presence at startup |
| Existing tests break when routes go protected | High | Medium | Update all test files in Phase 4 before merging |
| bcrypt rounds too high, login is slow | Low | Low | Salt rounds = 10 is standard; benchmark if needed |
| Token not expiring in tests causes flaky results | Medium | Low | Use short expiry (`60s`) in test env via `JWT_EXPIRES_IN` env var |

## 9. Open Questions

- Should `/api/users/:id` (the profile endpoint) be public or protected? Currently assuming protected.
- Is there a token refresh requirement, or is re-login on expiry acceptable?
- Should failed login attempts be rate-limited? (Recommend yes, but out of scope for this plan unless confirmed.)
