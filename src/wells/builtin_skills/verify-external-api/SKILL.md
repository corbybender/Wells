---
name: verify-external-api
description: Verify a library/API's CURRENT signature before using it — training data goes stale; use fetch_url/web_search instead of confident guessing.
---

Training data has a cutoff. A library's API can change (deprecated params, renamed methods, new required arguments) between that cutoff and the version actually pinned in this repo. Confidently writing code against a remembered-but-stale signature is a common, hard-to-detect failure mode — it looks plausible, passes a syntax check, and fails at runtime or in review.

When you are about to call a method/function from a third-party library you did not just read the source of in THIS repo:

1. **Check what's actually installed first** — grep the lockfile / manifest for the pinned version. A method that exists in the latest docs may not exist in the pinned version, and vice versa.
2. **Read the installed source directly if it's available locally** — the package is usually already on disk (site-packages / .venv / node_modules); read_file/grep it directly rather than trusting memory. This is faster and more reliable than a web fetch when it's available.
3. **If the source isn't available locally** (a remote API, a cloud SDK, a package installed elsewhere), use `web_search` to find the current docs or changelog, then `fetch_url` the specific page — don't guess from a search snippet alone, read the actual signature.
4. **For a REST/HTTP API you don't control**, check for an OpenAPI/Swagger spec or the provider's official docs page before assuming a request/response shape.

The cost of one fetch_url call is far lower than the cost of a wrong implementation discovered later in review or at runtime — this is the Verify-Before-Trust principle applied specifically to external dependencies, the single most common source of confidently-wrong code.
