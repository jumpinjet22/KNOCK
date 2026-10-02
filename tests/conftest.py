"""Collection-time test isolation.

`knock.api.app` computes its CSRF signing secret once at import time (see
`_get_or_create_csrf_secret()`), outside the per-request `Depends()`
pattern every other store uses -- so unlike `JSONFileSessionStore`/
`JSONLAuditLog`/`AuthStore`/`WebSessionStore` (all overridable per-test via
`app.dependency_overrides`), there's no way to redirect it from within a
test. Setting KNOCK_CSRF_SECRET here, at module level, runs before pytest
collects/imports any test module (and therefore before `knock.api.app`
itself gets imported anywhere), so the real `~/.local/share/knock/
csrf_secret` on the machine running the tests is never touched.
"""

import os

os.environ.setdefault("KNOCK_CSRF_SECRET", "test-only-csrf-secret-do-not-use-in-production")
