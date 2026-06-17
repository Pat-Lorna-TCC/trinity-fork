# Feature: First-Time Setup

## Overview
First-time setup wizard for admin password and API key configuration. On fresh install, users are redirected to `/setup` to set an admin password before accessing the platform. After login, admins can configure the Anthropic API key in Settings.

## User Story
As a platform administrator deploying Trinity for the first time, I want to be guided through initial configuration so that the platform is secured with a proper password and agents have access to the required API key.

## Requirements Reference
- **Requirement 11.4** - First-Time Setup Wizard (Phase 12.3)
- **#189** - Password complexity requirements (OWASP ASVS 2.1)
- Password: bcrypt-hashed, OWASP ASVS 2.1 complexity (12+ chars, uppercase, lowercase, digit, special, not common)
- Validation: `src/backend/utils/password_validation.py` — reusable module with `validate_password_strength()`
- API key: Stored in `system_settings` table, validated against Anthropic API

---

## Entry Points

### First Launch Flow
- **UI**: Any route visited on fresh install triggers redirect to `/setup`
- **API**: `GET /api/setup/status` (no auth) - Check if setup completed

### API Key Configuration Flow
- **UI**: `src/frontend/src/views/Settings.vue` - API Keys section
- **API**: `PUT /api/settings/api-keys/anthropic` (admin-only)

---

## Flow 1: First Launch Setup

### Frontend Layer

#### Router Guard
**File**: `src/frontend/src/router/index.js:165-220`

```javascript
// Cache for setup status check (avoid repeated API calls)
let setupStatusCache = null
let setupStatusCacheTime = 0
const SETUP_CACHE_DURATION = 5000 // 5 seconds

async function checkSetupStatus() {
  const now = Date.now()
  // Use cached value if recent
  if (setupStatusCache !== null && (now - setupStatusCacheTime) < SETUP_CACHE_DURATION) {
    return setupStatusCache
  }

  try {
    const response = await fetch('/api/setup/status')
    const data = await response.json()
    setupStatusCache = data.setup_completed
    setupStatusCacheTime = now
    return setupStatusCache
  } catch (e) {
    console.error('Failed to check setup status:', e)
    // Assume setup is completed if check fails (don't block access)
    return true
  }
}

// Navigation guard
router.beforeEach(async (to, from, next) => {
  // ... auth initialization check

  // Check setup status for login and protected routes
  if (!to.meta.isSetup) {
    const setupCompleted = await checkSetupStatus()

    // If setup not completed, redirect to setup page
    if (!setupCompleted) {
      // Allow access to public routes that don't need setup
      if (to.path === '/chat' || to.path.startsWith('/chat/')) {
        next()
        return
      }
      next('/setup')
      return
    }

    // If setup completed and trying to access setup page, redirect to login
    if (to.path === '/setup') {
      next('/login')
      return
    }
  }
  // ... rest of guard
})
```

#### Setup Route
**File**: `src/frontend/src/router/index.js:6-10`

```javascript
{
  path: '/setup',
  name: 'Setup',
  component: () => import('../views/SetupPassword.vue'),
  meta: { requiresAuth: false, isSetup: true }
}
```

#### Clear Setup Cache Export
**File**: `src/frontend/src/router/index.js:242-245`

```javascript
// Clear setup cache on successful setup
export function clearSetupCache() {
  setupStatusCache = null
  setupStatusCacheTime = 0
}
```

#### SetupPassword Component
**File**: `src/frontend/src/views/SetupPassword.vue`

**Key Features**:
- **Setup Token field** — required, instructions point to `docker compose logs backend`
- Password + Confirm Password fields with visibility toggle
- Password strength indicator (Weak/Fair/Good/Strong/Excellent)
- Client-side validation: token non-empty, min 8 chars, passwords must match
- Submits `setup_token`, `password`, `confirm_password` to `/api/setup/admin-password`

```javascript
// Submit handler
async function handleSubmit() {
  if (!isValid.value) return

  loading.value = true
  error.value = null

  try {
    await axios.post('/api/setup/admin-password', {
      setup_token: setupToken.value,
      password: password.value,
      confirm_password: confirmPassword.value
    })

    clearSetupCache()
    router.push('/login')
  } catch (e) {
    if (e.response?.status === 403) {
      const detail = e.response?.data?.detail || ''
      if (detail.toLowerCase().includes('token')) {
        error.value = 'Invalid setup token. Check server logs for the correct token.'
      } else {
        error.value = 'Setup has already been completed.'
        setTimeout(() => router.push('/login'), 2000)
      }
    } else {
      error.value = e.response?.data?.detail || 'Failed to set password. Please try again.'
    }
  } finally {
    loading.value = false
  }
}
```

**Validation Logic**:
```javascript
const isValid = computed(() => {
  return setupToken.value.length > 0 && password.value.length >= 8 && passwordsMatch.value
})
```

### Backend Layer

#### Setup Router
**File**: `src/backend/routers/setup.py`

**Router Registration** in `main.py:46, 294`:
```python
from routers.setup import router as setup_router
# ...
app.include_router(setup_router)
```

**Setup Token** (shared across workers via Redis, #1165):
```python
# Each worker has a candidate; the first to boot wins the SETNX claim and all
# workers read the single winner. Validation reads it live, so a token issued on
# one worker validates on any worker (prod runs uvicorn --workers 2).
_SETUP_TOKEN_KEY = "trinity:setup:token"
_candidate_token: str = secrets.token_urlsafe(24)

def ensure_setup_token():
    """Idempotently ensure a shared token exists in Redis; return it (or None if
    Redis is unreachable). The issuing worker prints it to the logs exactly once."""
    r = _get_redis()
    if r is None:
        return None  # setup blocked until Redis recovers — never a per-worker fallback
    issued = r.set(_SETUP_TOKEN_KEY, _candidate_token, nx=True)  # first-writer-wins
    token = r.get(_SETUP_TOKEN_KEY)
    if issued:
        logger.warning("TRINITY FIRST-TIME SETUP REQUIRED\nSetup token: %s\n...", token)
    return token
```

> **Why Redis, not a module global (#1165):** prod runs `uvicorn --workers 2`.
> A per-process `secrets.token_urlsafe(24)` differs per worker, so the operator
> copies one worker's token but `POST /api/setup/admin-password` load-balances
> and 403s ~50% of the time on the other worker. The shared Redis token (read
> live at validation time) removes the per-worker drift entirely. When Redis is
> unreachable, setup is **blocked** (`setup_available: false` + 503), not
> silently degraded to a per-worker token.

**Startup Token Emission** (in `main.py` lifespan handler, immediately after
`setup_logging()`):
```python
if _db.get_setting_value('setup_completed', 'false') != 'true':
    # ensure_setup_token() claims the shared token and prints it (once, on the
    # issuing worker). If Redis is down, the next GET /api/setup/status reissues
    # once it recovers — no restart needed.
    if _ensure_setup_token() is None:
        logger.error("FIRST-TIME SETUP REQUIRED but Redis unreachable — setup blocked.")
```

> **Why `logger.warning`, not `print` (#858):** the lifespan runs under uvicorn with
> stdout connected to a Docker log pipe (not a TTY). Without `ENV PYTHONUNBUFFERED=1`
> in `docker/backend/Dockerfile`, CPython block-buffers `print()` (~8KB) and the token
> never reaches `docker logs` — deadlocking fresh installs. The logging `StreamHandler`
> flushes after every record, so the token is delivered regardless, and it now flows
> through the structured JSON logger / Vector. The token is emitted *before* the
> event-bus and audit-write startup so a hang there can't suppress it. `PYTHONUNBUFFERED=1`
> (parity with `docker/scheduler/Dockerfile`) is the belt-and-suspenders fix for every
> remaining `print()`.

**Request Model**:
```python
class SetAdminPasswordRequest(BaseModel):
    """Request body for setting admin password."""
    password: str
    confirm_password: str
    setup_token: str  # Must match token printed in server logs at startup
```

#### GET /api/setup/status
```python
@router.get("/status")
async def get_setup_status():
    """
    Check if initial setup is complete. No auth required.

    Returns:
        - setup_completed: Whether the admin password has been set
        - setup_available: False while pending if Redis (token store) is down (#1165)
    """
    setup_completed = db.get_setting_value('setup_completed', 'false') == 'true'
    setup_available = True
    if not setup_completed:
        setup_available = ensure_setup_token() is not None  # probes Redis + self-heals
    return {
        "setup_completed": setup_completed,
        "setup_available": setup_available,
    }
```

#### POST /api/setup/admin-password
```python
@router.post("/admin-password")
async def set_admin_password(data: SetAdminPasswordRequest, request: Request):
    """
    Set admin password on first launch. No auth required, only works once.
    Requires the setup token printed to server logs at startup (prevents installation hijack).
    """
    # 1. Check setup not already completed
    if db.get_setting_value('setup_completed', 'false') == 'true':
        raise HTTPException(status_code=403, detail="Setup already completed...")

    # 2. Resolve the shared token; 503 if Redis is unreachable (don't fall back
    #    to a per-worker token — that is the #1165 bug).
    shared_token = ensure_setup_token()
    if shared_token is None:
        raise HTTPException(status_code=503, detail="Setup temporarily unavailable: Redis unreachable.")

    # 3. Validate setup token (constant-time compare to guard against timing attacks)
    if not secrets.compare_digest(data.setup_token, shared_token):
        raise HTTPException(
            status_code=403,
            detail="Invalid setup token. Check server logs for the setup token printed at startup."
        )

    # 3. Validate password length
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # 4. Validate passwords match
    if data.password != data.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    # 5. Hash password with bcrypt
    hashed_password = hash_password(data.password)

    # 6. Update admin user's password (creates user if doesn't exist)
    db.update_user_password('admin', hashed_password)

    # 7. Mark setup as completed
    db.set_setting('setup_completed', 'true')

    return {"success": True}
```

#### Password Hashing
**File**: `src/backend/dependencies.py:15-34`

```python
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, stored_password: str) -> bool:
    """Verify password against stored bcrypt hash.

    Security: Plaintext fallback removed (M-003, 2026-02-23).
    All passwords must be bcrypt hashed.
    """
    try:
        return pwd_context.verify(plain_password, stored_password)
    except Exception:
        # Invalid hash format - reject
        return False
```

**Security Note (M-003)**: The plaintext password fallback was removed on 2026-02-23. All passwords must be stored as bcrypt hashes. Invalid hash formats are rejected, returning `False` for authentication.

### Database Layer

#### User Password Update (Upsert Pattern)
**File**: `src/backend/db/users.py:129-162`

```python
def update_user_password(self, username: str, hashed_password: str) -> bool:
    """Update user's password hash, creating the user if it doesn't exist.

    For the admin user during first-time setup, this will create the user
    if it doesn't exist yet.

    Args:
        username: The username to update
        hashed_password: The bcrypt-hashed password

    Returns:
        True if the user was updated or created successfully
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Try to update existing user
        cursor.execute("""
            UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?
        """, (hashed_password, now, username))
        conn.commit()

        if cursor.rowcount > 0:
            return True

        # User doesn't exist - create it (for admin user during first-time setup)
        cursor.execute("""
            INSERT INTO users (username, password_hash, role, email, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (username, hashed_password, 'admin', username, now, now))
        conn.commit()
        return cursor.rowcount > 0
```

**Upsert Logic**:
1. First attempts UPDATE on existing user
2. If UPDATE affects 0 rows (user doesn't exist), performs INSERT
3. New admin users are created with role='admin' and username as email
4. This pattern ensures first-time setup works even on fresh deployments with no existing admin user

#### Settings Storage
**File**: `src/backend/db/settings.py:60-83`

```python
def set_setting(self, key: str, value: str) -> SystemSetting:
    """
    Set a system setting value (upsert).

    Creates the setting if it doesn't exist, updates if it does.
    Returns the updated setting.
    """
    now = datetime.utcnow().isoformat()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Use INSERT OR REPLACE for upsert
        cursor.execute("""
            INSERT OR REPLACE INTO system_settings (key, value, updated_at)
            VALUES (?, ?, ?)
        """, (key, value, now))
        conn.commit()

        return SystemSetting(
            key=key,
            value=value,
            updated_at=datetime.fromisoformat(now)
        )
```

**Get Setting Value** (lines 49-58):
```python
def get_setting_value(self, key: str, default: str = None) -> Optional[str]:
    """
    Get just the value of a setting.

    Returns the default if the setting doesn't exist.
    """
    setting = self.get_setting(key)
    if setting:
        return setting.value
    return default
```

#### Database Table
**File**: `src/backend/database.py:522-527`

```sql
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

**Settings Used**:
| Key | Value | Purpose |
|-----|-------|---------|
| `setup_completed` | `"true"` / `"false"` | Gate setup endpoint, redirect logic |
| `anthropic_api_key` | `"sk-ant-..."` | API key for Claude |

### Login Block During Setup

**File**: `src/backend/routers/auth.py`

**Setup Check Function** (lines 20-22):
```python
def is_setup_completed() -> bool:
    """Check if initial setup is completed."""
    return db.get_setting_value('setup_completed', 'false') == 'true'
```

**Admin Login Block** (lines 49-78):
```python
@router.post("/token", response_model=Token)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Login with username/password and get JWT token.

    Used for admin login (username 'admin' with password).
    Regular users should use email authentication.
    """
    # Block login if setup is not completed
    if not is_setup_completed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="setup_required"
        )
    # ... rest of login logic
```

**Email Login Request Block** (lines 153-158):
```python
# Block if setup is not completed
if not is_setup_completed():
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="setup_required"
    )
```

**Email Login Verify Block** (lines 210-215):
```python
# Block if setup is not completed
if not is_setup_completed():
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="setup_required"
    )
```

**Auth Mode Endpoint Reports Setup Status** (lines 27-46):
```python
@router.get("/api/auth/mode")
async def get_auth_mode():
    """
    Get authentication mode configuration.

    This endpoint requires NO authentication - it's called before login
    to determine which login options to show.

    Returns:
        - email_auth_enabled: Whether email-based login is enabled
        - setup_completed: Whether first-time setup is complete
    """
    email_auth_setting = db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower())
    email_auth_enabled = email_auth_setting.lower() == "true"

    return {
        "email_auth_enabled": email_auth_enabled,
        "setup_completed": is_setup_completed()
    }
```

---

## Flow 2: API Key Configuration

### Frontend Layer

#### Settings Page
**File**: `src/frontend/src/views/Settings.vue`

**API Key Section** (lines 23-127):
- Input field with show/hide toggle
- Test button - calls `/api/settings/api-keys/anthropic/test`
- Save button - calls `PUT /api/settings/api-keys/anthropic`
- Status indicator showing: Not configured / Configured (from settings/env)

**Key Methods** (lines 313-374):
```javascript
async function loadApiKeyStatus() {
  const response = await axios.get('/api/settings/api-keys')
  anthropicKeyStatus.value = response.data.anthropic || { configured: false }
}

async function testApiKey() {
  const response = await axios.post('/api/settings/api-keys/anthropic/test', {
    api_key: anthropicKey.value
  })
  apiKeyTestResult.value = response.data.valid
}

async function saveApiKey() {
  const response = await axios.put('/api/settings/api-keys/anthropic', {
    api_key: anthropicKey.value
  })
  anthropicKeyStatus.value = {
    configured: true,
    masked: response.data.masked,
    source: 'settings'
  }
}
```

### Backend Layer

#### API Keys Endpoints
**File**: `src/backend/routers/settings.py:361-588`

**GET /api/settings/api-keys** (line 394-430):
```python
@router.get("/api-keys")
async def get_api_keys_status(current_user: User = Depends(get_current_user)):
    """Get status of configured API keys. Admin-only."""
    require_admin(current_user)

    anthropic_key = get_anthropic_api_key()
    anthropic_configured = bool(anthropic_key)
    key_from_settings = bool(db.get_setting_value('anthropic_api_key', None))

    return {
        "anthropic": {
            "configured": anthropic_configured,
            "masked": mask_api_key(anthropic_key) if anthropic_configured else None,
            "source": "settings" if key_from_settings else ("env" if anthropic_configured else None)
        }
    }
```

**PUT /api/settings/api-keys/anthropic** (line 433-483):
```python
@router.put("/api-keys/anthropic")
async def update_anthropic_key(body: ApiKeyUpdate, current_user: User = Depends(get_current_user)):
    require_admin(current_user)

    key = body.api_key.strip()
    if not key.startswith('sk-ant-'):
        raise HTTPException(status_code=400, detail="Invalid API key format")

    db.set_setting('anthropic_api_key', key)
    return {"success": True, "masked": mask_api_key(key)}
```

**DELETE /api/settings/api-keys/anthropic** (line 486-519):
```python
@router.delete("/api-keys/anthropic")
async def delete_anthropic_key(current_user: User = Depends(get_current_user)):
    require_admin(current_user)

    deleted = db.delete_setting('anthropic_api_key')
    env_key = os.getenv('ANTHROPIC_API_KEY', '')

    return {
        "success": True,
        "deleted": deleted,
        "fallback_configured": bool(env_key)
    }
```

**POST /api/settings/api-keys/anthropic/test** (line 522-587):
```python
@router.post("/api-keys/anthropic/test")
async def test_anthropic_key(body: ApiKeyTest, current_user: User = Depends(get_current_user)):
    require_admin(current_user)

    key = body.api_key.strip()
    if not key.startswith('sk-ant-'):
        return {"valid": False, "error": "Invalid format"}

    # Make lightweight API call to validate
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=10.0
        )

        if response.status_code == 200:
            return {"valid": True}
        elif response.status_code == 401:
            return {"valid": False, "error": "Invalid API key"}
```

#### Key Retrieval Function
**File**: `src/backend/routers/settings.py:379-384`

```python
def get_anthropic_api_key() -> str:
    """Get Anthropic API key from settings, fallback to env var."""
    key = db.get_setting_value('anthropic_api_key', None)
    if key:
        return key
    return os.getenv('ANTHROPIC_API_KEY', '')
```

---

## Flow 3: Agent Uses Stored API Key

### Agent Creation
**File**: `src/backend/routers/agents.py:508-512`

```python
env_vars = {
    'AGENT_NAME': config.name,
    'AGENT_TYPE': config.type,
    'ANTHROPIC_API_KEY': get_anthropic_api_key(),  # Uses settings value OR env fallback
    # ...
}
```

### System Agent Service
**File**: `src/backend/services/system_agent_service.py:24, 180`

```python
from routers.settings import get_anthropic_api_key

# During system agent container creation:
env_vars = {
    # ...
    'ANTHROPIC_API_KEY': get_anthropic_api_key(),
}
```

---

## Side Effects

### Audit Logging

| Event | Type | Action | Details |
|-------|------|--------|---------|
| Password set | `setup` | `admin_password` | `result: success` |
| Setup blocked | `setup` | `admin_password` | `result: blocked, reason: already completed` |
| API key read | `system_settings` | `read_api_keys` | - |
| API key update | `system_settings` | `update_anthropic_key` | `key_masked: ...xxxx` |
| API key delete | `system_settings` | `delete_anthropic_key` | `deleted: true/false` |
| API key test | `system_settings` | `test_anthropic_key` | `valid: true/false` |

---

## Error Handling

| Error Case | HTTP Status | Message | Handling |
|------------|-------------|---------|----------|
| Setup already completed | 403 | "Setup already completed" | Frontend redirects to /login after 2s |
| Invalid setup token | 403 | "Invalid setup token. Check server logs..." | Frontend shows error, stays on form |
| Redis unreachable (#1165) | 503 | "Setup temporarily unavailable: ... cannot reach Redis" | Frontend shows "waiting for Redis" panel + polls |
| Missing setup_token field | 422 | Pydantic validation error | Form prevents submission |
| Password too short | 400 | "Password must be at least 8 characters" | Form validation |
| Passwords don't match | 400 | "Passwords do not match" | Form validation |
| Invalid API key format | 400 | "Invalid API key format. Keys start with 'sk-ant-'" | Inline error |
| API key invalid | N/A | `{valid: false, error: "..."}` | Test result display |
| Not admin | 403 | "Admin access required" | Redirect to dashboard |
| Login blocked (no setup) | 403 | "setup_required" | Frontend checks and redirects |

---

## Security Considerations

1. **Password Security**:
   - Bcrypt hashing with auto-configured work factor
   - **No plaintext fallback (M-003)**: Plaintext password comparison removed as of 2026-02-23. All passwords must be bcrypt hashed.
   - Invalid hash formats are rejected (returns authentication failure)
   - Minimum 8 character requirement
   - Setup endpoint only works ONCE

2. **API Key Security**:
   - Never exposed in full (masked in responses)
   - Format validation (`sk-ant-` prefix)
   - Admin-only access to all API key endpoints
   - Fallback to environment variable if not in settings

3. **Setup Endpoint Protection** (SEC #177):
   - No network auth required (must work on fresh install)
   - **Shared setup token (#1165)**: Generated via `secrets.token_urlsafe(24)`, stored in Redis (first-writer-wins) so all uvicorn workers share one value, printed ONLY to server logs. An attacker without local server access cannot complete setup. When Redis is down, setup is blocked (503) rather than degraded to a per-worker token.
   - Constant-time token comparison (`secrets.compare_digest`) prevents timing attacks
   - Self-disabling after first use via `setup_completed` flag
   - Audit logged even on blocked attempts

4. **Login Block**:
   - Login endpoint returns 403 with `setup_required` until admin password set
   - Prevents access with default password

---

## Testing

### Prerequisites
- Fresh database (delete `~/trinity-data/trinity.db`) or reset `setup_completed` setting
- Backend and frontend running

### Test Steps

**Flow 1: First-Time Setup**

1. **Delete existing setup flag**
   ```sql
   DELETE FROM system_settings WHERE key = 'setup_completed';
   ```

2. **Restart the backend** and check logs for the setup token:
   ```bash
   docker compose logs backend | grep "Setup token"
   ```
   - **Expected**: a structured JSON log line whose `message` contains
     `Setup token: <token>` (emitted at `WARNING` level since #858).
   - **Prod (#1165)**: production runs uvicorn with `--workers 2`, but the token is
     shared via Redis (first-writer-wins), so exactly **one** worker logs the token
     and it validates regardless of which worker handles the request. If Redis is
     down, no token is logged and the setup UI shows "waiting for Redis"; the token
     is issued automatically once Redis recovers (no restart needed).

3. **Visit any page** (e.g., `http://localhost/`)
   - **Expected**: Redirect to `/setup`
   - **Verify**: URL shows `/setup`, setup form displays token field + password fields

4. **Enter wrong setup token**, valid passwords
   - **Expected**: 403 error, "Invalid setup token" message shown

5. **Enter correct token from logs, try weak password** (less than 8 chars)
   - **Expected**: Submit button disabled (client-side validation)

6. **Enter correct token, mismatched passwords**
   - **Expected**: "Passwords do not match" indicator, submit disabled

7. **Enter correct token, valid matching password** (8+ chars)
   - **Expected**: Submit enabled — click "Set Password & Continue"

8. **After successful setup**
   - **Expected**: Redirect to `/login`
   - **Verify**: Can log in with `admin` / new password

9. **Try accessing /setup again**
   - **Expected**: Redirect to `/login` (setup already done)

**Flow 2: API Key Configuration**

1. **Login as admin**, navigate to Settings

2. **Check initial status**
   - **Expected**: "Not configured" warning if no env var

3. **Enter invalid key format** (e.g., "test123")
   - Click Test
   - **Expected**: "Invalid format" error

4. **Enter valid format but invalid key** (e.g., "sk-ant-fake123")
   - Click Test
   - **Expected**: "Invalid API key" error

5. **Enter valid API key**
   - Click Test
   - **Expected**: "API key is valid!" success

6. **Save the key**
   - Click Save
   - **Expected**: Status changes to "Configured (from settings)"

7. **Create an agent**
   - **Verify**: Agent can use Claude (API key injected)

### Edge Cases

- **Multiple setup attempts**: Second POST to `/api/setup/admin-password` returns 403
- **Env fallback**: Delete key from settings, env var should be used
- **Non-admin access**: Settings page returns 403 for non-admin users

### Cleanup

```sql
-- Reset to fresh state
DELETE FROM system_settings WHERE key IN ('setup_completed', 'anthropic_api_key');
UPDATE users SET password_hash = 'changeme' WHERE username = 'admin';
```

### Status
- First-Time Setup: **Working** (Implemented 2025-12-23)
- API Key Configuration: **Working** (Implemented 2025-12-23)

---

## Related Flows

### Upstream
- None (this is the entry point for fresh installations)

### Downstream
- **Agent Lifecycle**: Uses stored API key via `get_anthropic_api_key()`
- **System Agent**: Uses stored API key for trinity-system operations
- **Authentication**: Login blocked until setup completed

---

## Revision History

| Date | Change | Details |
|------|--------|---------|
| 2025-12-23 | Initial documentation | First-time setup and API key configuration flows |
| 2026-01-14 | Bug fix: Admin user upsert | Fixed `update_user_password()` to create admin user if it doesn't exist. Previously, on fresh deployment with empty ADMIN_PASSWORD env var, the UPDATE query affected 0 rows but setup_completed was still set to true, leaving users unable to login. The method now uses an upsert pattern: UPDATE first, then INSERT if no rows affected. See `src/backend/db/users.py:129-162`. |
| 2026-01-23 | Line number verification | Updated all line numbers to match current codebase. Verified: setup.py endpoints (22-34, 37-81), auth.py login blocks (20-22, 49-78, 153-158, 210-215), dependencies.py password hashing (15-37), db/users.py upsert (129-162), db/settings.py (49-83), router/index.js guards (165-220, 242-245), SetupPassword.vue (166-176, 205-207, 209-236). Added documentation for email auth login blocking and password strength validation. |
| 2026-02-23 | Security Fix M-003 | Removed plaintext password fallback from `verify_password()` in dependencies.py:24-34. All passwords must now be bcrypt hashed. Invalid hash formats are rejected. Updated Password Hashing section and Security Considerations. |
| 2026-03-26 | Security Fix SEC #177 | Added single-use setup token to prevent installation hijack. Token generated via `secrets.token_urlsafe(24)` at startup, printed to server logs, required in `POST /api/setup/admin-password`. Frontend adds setup token field with instructions to check `docker compose logs backend`. Constant-time comparison guards against timing attacks. |
