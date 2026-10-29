# Reproduction scope

The default product and ordinary tests use explicitly synthetic data. They can
build their own local artifacts without the author's data cache, model cache or
absolute workspace paths. Installing locked dependencies and pulling container
base images requires network access; subsequent offline tests do not call model
providers or official data services.

Use `python -m scripts.verify_offline` for engineering checks. Run
`python -m scripts.verify_container_product --output .local/container-check` on
a Docker host to build without Docker build-cache reuse, exercise a complete
version-authorized synthetic investigation, replay its source and test persistence
across a product restart. The command never enables the real model profile.

## Official sources and real models

On Windows, use a short checkout path. Deeply nested checkout and test paths can
exceed the local Qdrant/SQLite path limit; the retained first public-copy run
demonstrates this restriction. A shorter independent checkout passed, and the
failure is not hidden by skipping that test.

Source URLs, schemas and acquisition instructions are in `docs/data/`,
`ingestion/` and the module learning guides. Capture public records through the
registered source interface (`python -m ingestion capture SOURCE_ID`), then
verify, parse, index and publish using the documented P1-02 through P1-06 commands.
Download retrieval weights under their own licenses; they are not bundled.
The same URL fetched later may return different bytes and therefore a different
release identity.

Model credentials belong only in a local ignored environment. Neither importing
the project nor running ordinary tests is authorization for a paid batch. The
historical approval records do not grant a new user permission to rerun a batch.

## Historical results

The original confirmation retained 140 observations, 116 model requests and 80
predeclared actual human readings. Acceptance was **not passed**. Full atomic
measurement for 19 claim-level readings and old ablation semantics remain incomplete.
The later development guards do not turn this result into a fresh confirmation.

Original source snapshots, provider request/response artifacts and actual human
reply payloads are not all distributed. Local paths in public reports are
navigation descriptions, not a promise that the underlying private artifact is
included. Some JSON paths are replaced with explicit private-audit notices.
`PUBLICATION-MANIFEST.json` records each transformation. Original frozen digests
must be checked against the original artifacts; never recompute a sanitized
derivative's digest and present it as the original experiment identity.

Consequently a fresh clone can reproduce the synthetic engineering workflow,
but cannot independently reconstruct every historical real-model result from
summary JSON alone. No full historical replication, Docker/user acceptance or
GitHub publication is implied merely by the existence of these instructions.
