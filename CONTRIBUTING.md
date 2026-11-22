# Contributing

Bug reports, documentation fixes and small pull requests are welcome. For a larger change, open an issue first so we can agree on the shape before you write code. This is a one-person project, so replies can take a while and I cannot promise a review time.

## Set up

```bash
git clone https://github.com/SouparneyaC/kalshi-orderbook-dashboard.git
cd kalshi-orderbook-dashboard
python3.12 -m venv venv
venv/bin/pip install -r requirements-lock.txt ruff
```

The tests replay a simulated Kalshi stream, so they need no API key and no network.

```bash
venv/bin/python -m unittest discover -s tests -t .
venv/bin/ruff check .
```

## What a good pull request has

- One change, with a description of what it does and why
- A test for new behavior, or a note on why there is none
- Updated docs when settings, endpoints or behavior change
- A line naming the measurements it could affect, if it touches the hot path

`tests/test_golden.py` checks that dashboard payloads and database rows stay the same across refactors. If it fails and the change is intended, say so in the pull request and update `tests/golden/baseline.json` with it.

## The numbers

Every figure in the README and in `docs/benchmarks.md` is a row in `numbers/`, with a window, a source and a check status. The plots in `docs/img/` are drawn from those rows by `bench/plots/make_plots.py`. Change a number by changing its row, then redraw:

```bash
venv/bin/pip install -r requirements-bench.txt
venv/bin/python bench/plots/make_plots.py
```

A measurement from a different machine is a new row with its own source. Please do not edit an existing row to match a different run.

## Out of scope

- Order placement or any trading logic. The service reads the feed and nothing else.
- Support for other exchanges
- A login system inside the app

## Written with AI tools

Parts of this repository were written with Claude, an AI model from Anthropic, working with the repository owner. Contributions made with AI tools are welcome if you have read, run and understood what you submit.

## Conduct

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
