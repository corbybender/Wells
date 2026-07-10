---
name: add-provider-profile
description: How to add a new model provider profile to the Wells harness (for contributors extending provider support).
---

To add a new provider profile to Wells:

1. Provider profiles are named configs selected via `MODEL_PROFILES` /
   `MODEL_PROFILE`. The factory lives in `src/wells/providers.py`.
2. Most OpenAI-compatible endpoints need no code — just three env vars:
   `MODEL_<name>`, `API_KEY_<name>`, `BASE_URL_<name>`, then add `<name>` to
   `MODEL_PROFILES`.
3. For a provider needing a distinct client kind (e.g. Anthropic, Bedrock),
   add a branch to `providers.load_profile()` and document the optional
   pip package in the README provider table.
4. Pin exact dollar rates with `MODEL_PRICE_<name>=<in>,<out>` ($/1M tokens)
   so the status-bar cost estimate is accurate.
5. Add a test to `tests/test_providers_and_settings.py` covering profile
   resolution + label.
