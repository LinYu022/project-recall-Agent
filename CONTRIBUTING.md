# Contributing

Use Python 3.12 and the committed uv lockfile. Install the frontend with its
committed pnpm lockfile. Run `python -m scripts.verify_offline`, `python -m mypy portal`,
and `pnpm --dir frontend run build`. PostgreSQL integration tests require a dedicated
disposable database and `RECALL_TEST_DATABASE_URL`.

Keep model and official-source network experiments separate from ordinary tests.
Never commit credentials, source caches, full complaints, VINs, session files or databases.
Preserve failed runs and original judgments. A passed engineering test must not be
reported as a human semantic judgment or as model quality acceptance.

Explain the change and its validation. Add meaningful positive and negative checks
when changing authorization, source provenance, state transitions or publication guards.
Do not regenerate historical results to make a change look successful.
