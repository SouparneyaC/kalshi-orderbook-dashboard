# The numbers behind the measurements

Every number in the write-up of this service is a row in one of these files, with the window it covers, where it came from, and a check status. Nothing is typed into the text by hand.

| File | What it holds |
|---|---|
| `numbers.csv` | The 48-hour capture (2026-11-04 16:11 UTC to 2026-11-06 16:11 UTC, 200 books, commit 48fc24a): messages, books, CPU, memory, latency percentiles, health |
| `numbers_capacity.csv`, `capacity_rederived.csv` | The capacity test of 2026-11-13 to 11-14, 200 to 1,000 books at night with no browser open |
| `numbers_profile.csv` | The profile of the live service on the evening of 2026-11-16 (perf and py-spy, 400 books, no browser open), the per-minute checks on the tail of Kalshi-to-receipt, and the sizes of Python integers read on the server |
| `numbers_connect.csv` | TCP connect time to Kalshi from the server |
| `numbers_coverage.csv`, `numbers_mix.csv`, `numbers_trades.csv` | What trades, and how much of the next window's dollars a set of books covers, from recorded trades |
| `numbers_sketch.csv`, `numbers_f2.csv`, `numbers_distinct.csv` | The streaming summaries tested against exact counts on recorded trades |
| `numbers_book.csv`, `numbers_external.csv` | The book layout and the instance price |

## Columns

`id`, `value`, `unit`, `window` (the time span or the recording), `source` (the table or tool it came from), `check`, `note`.

## The `check` column

| Value | Meaning |
|---|---|
| `confirmed` | Two independent derivations agree within the tolerance written in the note, or the value is true by definition |
| `single_source` | Derived once. Not yet checked a second way |
| `carried` | Taken from another record and not re-derived. The price of the instance is the only one |

Percentiles are read from merged histogram buckets and never averaged across minutes.

## Where a number was measured

The write-up uses figures measured on the server only. Rows measured on the author's laptop are kept as a record and carry `LAPTOP` in the `note` column. They are not used in the write-up.

## Source paths

Some `source` and `note` cells name files that start with `experiments/`, `bench/` or `project/`. Those are in the author's private working repository, which also holds the scripts that built these files. This repository holds the results only.
