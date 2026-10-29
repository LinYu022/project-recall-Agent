# Recall Evidence Agent

A local research prototype for investigating US vehicle recalls and available
official evidence. It preserves source versions, citation locations, unknown
conditions, user approvals and execution history.

**Model quality acceptance has not passed.** The confirmation experiment retained
140 cells and 116 requests. Known failures include citation mismatches, omitted
risks and incomplete explanations. Engineering checks and simulated UI demos do
not certify a vehicle's safety or establish agent superiority.

## Start the local product

Install Docker Engine with Compose. Set `RECALL_DB_PASSWORD` to a random hex string
and `RECALL_CONTROL_TOKEN_OWNERS` to a JSON object mapping a random local token to
a stable local user name. These are local application credentials, not model keys.

```sh
docker compose config --quiet
docker compose up --build
```

Open <http://127.0.0.1:8017>. Enter your configured local token. The default profile
creates synthetic examples and uses simulated evidence-insufficient generation;
it makes no external model requests. Stop with `docker compose down` to preserve
the database and artifact volumes.

For native installation and detailed operations, see [the product guide](docs/product-guide.md).
The frontend requires Node.js 22.12+ and pnpm 11.25.0. Python requires 3.12 and uv.

```sh
uv sync --locked --python 3.12
pnpm --dir frontend install --frozen-lockfile --ignore-scripts
pnpm --dir frontend run build
uv run --locked --offline python -m scripts.verify_offline
```

Use a separate PostgreSQL database for integration tests. Real-model experiments
require credentials, an explicit request budget and a newly frozen experiment;
ordinary CI does not invoke them.

## Evidence, results and limits

- [Product operation](docs/product-guide.md) and [recorded demonstrations](docs/experiments/P1-17-product.md).
- [Confirmation result: not passed](docs/experiments/P1-16-confirmation-results.md).
- [One actual unsupported-citation witness](docs/experiments/public-confirmation-witness.json),
  retaining the exact conclusion, its actual source and the historical verdict.
- [Subsequent development repairs](docs/experiments/P1-16-quality-repairs-v1.md).
- [Reproduction scope and historical artifacts](docs/reproducibility.md).
- [Architecture](docs/architecture/module-map.md), [learning materials](docs/learning/P1-17.md),
  [data sources](docs/data/exploration-sources.json), and [third-party boundaries](THIRD_PARTY_NOTICES.md).

The public snapshot is a privacy-filtered derivative. It excludes local caches,
credentials and private human reply payloads, and does not contain the development
Git history. `PUBLICATION-MANIFEST.json` identifies the exported bytes and changed
artifacts. Historical frozen hashes refer to original retained artifacts, not
to a rewritten public derivative.

MIT license for original code. See [LICENSE](LICENSE), [CONTRIBUTING.md](CONTRIBUTING.md)
and [SECURITY.md](SECURITY.md). No GitHub URL, release tag or remote verification is
claimed until an actual upload has completed.
