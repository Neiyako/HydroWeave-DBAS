# DBAS

**Data Asset Management & Orchestration for Scientific Data**

[![CI](https://github.com/Neiyako/HydroWeave-DBAS/actions/workflows/ci.yml/badge.svg)](https://github.com/Neiyako/HydroWeave-DBAS/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-orange)](pyproject.toml)

**English** · [简体中文](README.md)

---

Catalog scattered scientific data (ERA5 / NetCDF) across external drives, working directories and download folders into a **searchable, verifiable, traceable, recoverable** local catalog database — and derive a chunked Zarr array layer on top of it.

> **DBAS manages data assets; it does not replace your data warehouse.** Source volumes stay strictly read-only. A SQLite catalog stores volumes, files, metadata, datasets, quality evidence and task state; scanning, verification, backup and the task queue keep that catalog maintainable over time.

**This repository covers data management and acquisition only.** Experiment computation, error analysis and plotting live in the separate [HydroWeave](https://github.com/Neiyako/HydroWeave) experiment repository.

---

## Table of Contents

- [What It Solves](#what-it-solves)
- [Design](#design)
- [Measured Performance](#measured-performance)
- [Quick Start](#quick-start)
- [Languages & Dependencies](#languages--dependencies)
- [Project Structure](#project-structure)
- [Command Reference](#command-reference)
- [Testing & CI](#testing--ci)
- [Scope & Known Limitations](#scope--known-limitations)
- [Documentation](#documentation)
- [License](#license)

---

## What It Solves

| Symptom | What DBAS does | Command |
|---|---|---|
| No idea what is on the drive | Inventory into a searchable catalog by dataset/variable/time/space | `scan --mode inventory`, `dataset list` |
| Paths break after moving machines | Stable identity = `volume_id` + volume-relative path | `volume register --id` |
| Files may be silently corrupted | Readability check + streaming SHA-256 | `verify`, `checksum` |
| Queries are slow; whole files must be opened | Zarr array layer chunked by query pattern | `zarr plan/enqueue/run` |
| No way to tell if data changed | Per-chunk manifest hash | `zarr verify` |
| The catalog itself can break | Online backup + integrity check | `db backup`, `db check` |

## Design

### Two independent evidence chains

| Layer | Unit of storage | Question answered | Evidence |
|---|---|---|---|
| **Source / analysis volume** | A single `.nc` file | What is this, is it broken, can it be reproduced? | Per-file SHA-256 + QC rules |
| **Array layer (Zarr)** | One store directory | How fast can a time/space window be retrieved? | Manifest digest |

Source data is never modified. The array layer is derived and can be deleted and rebuilt at any time.

### Chunking is a function of the query pattern

Public ERA5→Zarr datasets (e.g. [arco-era5](https://github.com/google-research/arco-era5)) hard-code chunk shapes optimized for "one full field at one time step". Copying that makes "single-point long time series" an order of magnitude slower. DBAS requires an **explicit query pattern** and derives chunking from it:

| target | Typical query | time | space | level |
|---|---|---|---|---|
| `field_series` | One field at one time step | 1 | full | all |
| `point_series` | Single point / few stations, long series | full | 8×8 | all |
| `area_stats` | Area means, temporal statistics | 128 | 32 | all |
| `level_profile` | Vertical profiles by pressure level | 24 | full | 1 |

Multiple layouts for the same source can coexist; outputs are separated per target so they never overwrite each other.

## Measured Performance

426 MB synthetic ERA5-shaped data (`240 × 37 × 100 × 120`, f4), best of 5 runs, macOS:

| Query | Raw NetCDF | `field_series` | `point_series` |
|---|---|---|---|
| Single-point long time series | 28.9 ms | 215.3 ms | **3.7 ms** |
| One full field at one step | **3.1 ms** | 3.8 ms | 191.1 ms |

- **Right layout**: `point_series` is **58×** faster than `field_series` and **7.9×** faster than reading NetCDF directly;
- **Wrong layout is ruinous**: for the same data, a full-field query under `point_series` is **50×** slower;
- **Zarr is no silver bullet**: a full-field query still takes 3.8 ms under `field_series` versus 3.1 ms for raw NetCDF —
  sequential full-file reads are what NetCDF is good at; Zarr pays off for **random slicing** and **concurrent queries**;
- Data read back from both layouts is **bit-identical** to the source (`np.array_equal` all True).

**When is an array layer worth it:** when you repeatedly slice by point, region or level. For one-off full reads, raw NetCDF is better.

## Quick Start

```bash
git clone https://github.com/Neiyako/HydroWeave-DBAS.git
cd HydroWeave-DBAS

./scripts/setup.sh --with-zarr     # create venv, install, self-check
./bin/dbas --help
```

> Always launch via `./bin/dbas`, never `python -m dbas`. See [deployment docs](docs/deployment.md).

End-to-end in 3 minutes (no network, no real data):

```bash
# 1) synthetic data → catalog → scan → parse
.venv/bin/python scripts/demo/make_demo_volume.py
./bin/dbas volume register data/demo_volume --id demo
./bin/dbas scan --volume demo --mode inventory --enqueue-metadata
./bin/dbas scan --volume demo --mode metadata
./bin/dbas status

# 2) derive a Zarr array layer and verify the evidence
./bin/dbas zarr enqueue --volume demo --target point_series
./bin/dbas zarr run
./bin/dbas zarr verify --store point_series/2023/era5_demo_00.zarr   # expect ✔ 一致 (consistent)
```

Connecting a real data drive:

```bash
./bin/dbas volume register /Volumes/ERA5HD --id era5hd
./bin/dbas scan --volume era5hd --mode inventory --enqueue-metadata
./bin/dbas scan --volume era5hd --mode metadata --budget 600 --batch-size 50
./bin/dbas dataset assign --volume era5hd
./bin/dbas profile --volume era5hd --limit 100
./bin/dbas qc run --volume era5hd
./bin/dbas zarr enqueue --volume era5hd --target point_series && ./bin/dbas zarr run
./bin/dbas verify --volume era5hd && ./bin/dbas db check && ./bin/dbas db backup
```

Full instructions, expected output and troubleshooting: **[docs/deployment.md](docs/deployment.md)**.

## Languages & Dependencies

**Python ≥ 3.11** (CI covers 3.11 / 3.12 / 3.13), with Shell for the launcher and setup script.

| Language | Purpose | Size |
|---|---|---|
| Python | All business logic (CLI, scanning, scheduling, derivation, fetching) | ~7,500 lines |
| Shell | `bin/dbas` launcher, `scripts/setup.sh` deployment script | ~110 lines |

Runtime dependencies (installed automatically by `pip install -e .`):

| Package | Purpose |
|---|---|
| `netCDF4` | Read/write NetCDF; header-only parsing |
| `numpy` / `pandas` | Numeric and tabular processing |
| `cftime` | CF time-axis decoding |
| `PyYAML` | Configuration parsing |
| `psutil` | Resource watermark protection (RSS / available memory) |

Optional extras:

| extras | Contents | Used for |
|---|---|---|
| `[zarr]` | `xarray` `dask` `zarr>=3.0` | Array layer derivation (recommended) |
| `[eda]` | `xarray` `dask` `flox` | `subset` region cropping |
| `[fetch]` | `cdsapi` | CDS downloads |
| `[dev]` | `pytest` `ruff` | Testing and linting |

Install everything: `./scripts/setup.sh --all`

## Project Structure

```text
HydroWeave-DBAS/
├── .github/workflows/ci.yml    CI: lint / test(3.11-3.13) / smoke / deployment
├── bin/dbas                    Launcher (PYTHONPATH=src, avoids hidden .pth issue)
├── configs/
│   ├── system.yaml             System config: budgets, watermarks, scan rules, zarr params
│   ├── fetch/                  CDS request templates
│   └── constraints-py313.txt   Verified dependency version snapshot
├── src/
│   ├── dbas/                   —— Application layer: data management & acquisition ——
│   │   ├── cli.py              Unified command entry point (14 subcommands)
│   │   ├── scanner/            inventory / incremental / metadata / verify / checksum
│   │   ├── scheduler/          Atomic claim, budget, batching, retry, stale-task reclaim
│   │   ├── storage/            Volume identity, mount helpers, SQLite online backup
│   │   ├── resource_guard/     RSS / available-memory soft protection
│   │   ├── fetch/              CDS API downloads + redacted capture diagnostics
│   │   ├── reports/            Markdown inventory & QC reports (atomic writes)
│   │   └── zarr_export/        Array layer: chunk planning, store writes, manifest hashing
│   └── dbas_core/              —— Core library (inlined; no separate install) ——
│       ├── catalog/            SQLite repository for volumes/files/tasks/quality evidence
│       ├── config.py           Config loading and project-root discovery
│       ├── inspection/         Read-only NetCDF header parsing
│       ├── qc/                 File-level quality rules (axes, time axis, duplicate variables)
│       └── analysis/           Dataset detection, content profiling, region cropping
├── scripts/
│   ├── setup.sh                One-shot idempotent setup
│   ├── demo/                   Synthetic volume generators
│   └── cds_*                   Capture diagnostics (optional)
├── tests/                      158 cases, all synthetic data, no network
├── docs/                       architecture / deployment / operations / zarr-layer / agent-prompts
├── data/                       Runtime output: catalog.db, reports, backups, arrays (git-ignored)
├── pyproject.toml              Packaging, dependencies, pytest and ruff config
└── LICENSE                     MIT
```

**Layering**: `dbas` (application) handles orchestration and data management; `dbas_core` (core library) provides the catalog, parsing, QC and profiling. The core library is inlined in this repository, so `git clone` is immediately installable with no external git dependencies.

### Data flow

```text
Source volume (read-only)
   │ volume_id + rel_path
   ▼
SQLite catalog ── scan / parse / verify / profile / QC
   │
   ├── subset ──────> analysis volume (.nc, region cropping)
   ├── zarr_export ─> array layer (.zarr, manifest hash)
   └── fetch ───────> downloads volume (auto-registered after CDS download)
```

## Command Reference

| Command | Purpose |
|---|---|
| `dbas status` | Overview of volumes, files, parsing, profiling, task queue |
| `dbas volume register/list` | Register and inspect data volumes |
| `dbas scan --mode inventory\|incremental\|metadata\|verify` | Inventory / incremental / parse / verify |
| `dbas verify` / `dbas checksum` | Readability check / streaming SHA-256 |
| `dbas tasks list/show/prune` | Task queue operations |
| `dbas dataset list/assign` | Dataset grouping and detection |
| `dbas profile` / `dbas qc run\|report` | Content profiling / quality evidence |
| `dbas subset` | Region cropping into an analysis volume |
| `dbas zarr plan/enqueue/run/verify/info` | Array layer derivation and manifest reconciliation |
| `dbas db check/backup/rebuild` | Database integrity / online backup / rebuild |
| `dbas report inventory` | Markdown operations report |
| `dbas fetch plan/enqueue/run` | CDS download management |

Every command supports `--help`.

## Testing & CI

```bash
.venv/bin/python -m pytest --cov    # 158 passed, 80% coverage gate
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy            # type check (src only)
```

Tests use temporary directories and synthetic NetCDF only — **no network, no real ERA5 volume needed**. Coverage includes the CLI, scanning, scheduling, the fetch chain, online backup and failure cleanup, plus array-layer chunking, round-trip consistency, manifest hashing, catalog registration and clobber protection.

Quality gates (identical locally and in CI; any failure turns the build red):

| Check | Tool | Gate | Measured |
|---|---|---|---|
| Static analysis | `ruff` | 0 issues | passing |
| Type checking | `mypy` | 0 errors (`src`, 40 files) | passing |
| Test coverage | `pytest-cov` | **≥ 80%** (branch) | 81.6% |

The `ruff` rule set is deliberately conservative (defects only, not style) so the gate does not rot into permanent red over formatting taste. The coverage threshold is enforced in `pyproject.toml` (`fail_under`) — it is a hard gate, not an advisory.

CI runs five jobs:

- **Lint**: `ruff`;
- **Type check**: `mypy`;
- **Test**: Python 3.11 / 3.12 / 3.13 matrix with the 80% coverage gate;
- **Smoke**: installs per the deployment docs and runs a real end-to-end flow, asserting via SQLite that parsing actually persisted — guarding against "exit code 0 but nothing happened";
- **Deployment docs**: runs `setup.sh` and then the full test suite.

## Scope & Known Limitations

**Out of scope**: the experiment and error-analysis layer (cloud-water integrals, precipitable water, saturation vapour pressure, error metrics, formula audits, plotting), plus general EDA, cleaning, feature engineering, Parquet export and modelling interfaces.

The retained `profile` (sampled profiling: missing ratio, value range, sample statistics) and `qc` (file-level rules for axes, time axis and duplicate variables) **belong to data management** — they answer "is this data usable", not research computation.

Known limitations:

- The array layer is currently **one store per source file**; merging into a single time axis requires an extra consolidate step;
- `zarr enqueue/run` has no test coverage against real ERA5 volumes yet — all array-layer evidence comes from synthetic data;
- Zarr v3 consolidated metadata is still experimental; expect a `ZarrUserWarning` on import (harmless);
- A full-volume `checksum` baseline must be run in off-peak batches.

## Documentation

| Document | Contents |
|---|---|
| [Deployment](docs/deployment.md) | From zero to working, with expected output and troubleshooting |
| [Agent prompts](docs/agent-prompts.md) | Ready-to-use prompts for deploying via an AI agent |
| [Architecture](docs/architecture.md) | Module boundaries and data flow |
| [Operations](docs/operations.md) | Routine maintenance, backup/restore, pitfalls |
| [Array layer design](docs/zarr-layer.md) | Chunking strategy, manifest hashing, benchmarks |
| [Fetch prior art](docs/prior-art-era5-fetch.md) | Trade-offs versus cdsapi / cdsswarm |
| [Changelog](CHANGELOG.md) | Release notes for each version |
| [Contributing](CONTRIBUTING.md) | Scope, workflow and acceptance criteria |
| [Code of Conduct](CODE_OF_CONDUCT.md) | Community behaviour standards |

## Contributing

Issues and pull requests are welcome. Please make sure your change fits this
repository's scope (data management and acquisition) and that all three gates
pass locally:

```bash
.venv/bin/python -m ruff check src tests scripts   # 0 issues
.venv/bin/python -m mypy                           # 0 errors
.venv/bin/python -m pytest --cov                   # all green, coverage >= 80%
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for details. For security issues, use the
[private channel](https://github.com/Neiyako/HydroWeave-DBAS/security/advisories/new)
instead of a public issue.

## Citation

If DBAS helps your research, please cite it using the bundled
[CITATION.cff](CITATION.cff):

```bibtex
@software{dbas2026,
  title   = {DBAS: Data Asset Management and Task Orchestration for Scientific Data},
  author  = {Neiyako},
  year    = {2026},
  version = {1.0.0},
  url     = {https://github.com/Neiyako/HydroWeave-DBAS},
  license = {MIT}
}
```

## License

[MIT](LICENSE) © 2026 Neiyako
