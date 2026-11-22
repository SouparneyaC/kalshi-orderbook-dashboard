# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). While the version is 0.y.z, the public interface can change between minor versions. The public interface is the environment variables, the HTTP endpoints and the column layout of the files in `numbers/`.

## [Unreleased]

## [0.1.0] - 2026-11-21

The first tagged release. The code in `app/` is the code that the measurements in `numbers/` were taken on.

### Added

- Continuous integration: tests and Ruff on every push, plus a container build and health check
- CodeQL analysis, and Dependabot updates for pip, GitHub Actions and Docker
- `Dockerfile` and `compose.yaml` for trying the service without installing Python
- Plots of latency, CPU use, the profile and the capacity test in `docs/img/`, drawn from `numbers/` by `bench/plots/make_plots.py`
- `docs/` pages on architecture, configuration, deployment and benchmarks
- `requirements-lock.txt`, the versions the 73 tests passed with
- `pyproject.toml` with the Python floor (3.12) and lint settings
- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CITATION.cff`, and issue and pull request templates

### Changed

- The README leads with the pipeline and its measurements. The configuration and endpoint tables moved to `docs/configuration.md`

### Earlier history, before the first tag

- 2026-11-16: added `numbers/`, the measured values with a source and a check for each
- 2026-11-15: added the accuracy audit against Kalshi's snapshots, the per-minute measurement store and the pipeline page
- 2026-10-26: first public release, then moved the code into `app/`

[Unreleased]: https://github.com/SouparneyaC/kalshi-orderbook-dashboard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SouparneyaC/kalshi-orderbook-dashboard/releases/tag/v0.1.0
