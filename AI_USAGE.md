# AI Usage Disclosure

## Tools used
Claude Code (Anthropic), run as an agent.

## What I used them for
Most of the first version of the solution. In one session the agent read the assignment and the data generator, picked the assignment, designed the schema and the PII approach, and wrote the code in `solution/pipeline/`, the tests in `solution/tests/` (including `reference.py`), and the first drafts of `SUBMISSION.md` and this file. It also did the profiling and the performance fixes on the large dataset.

## What I wrote or decided myself
TODO: write this part yourself, in your own words. Things worth covering: which decisions in the table in `SUBMISSION.md` you checked and agree with, anything you changed or rewrote after reading the code, and any tests you added.

## Where the AI was wrong or unhelpful
- Speed. The first version of the ledger table was ordered by `event_id` with two extra indexes and SQLite's small default cache. It gave correct output but took about 7 minutes on the large data. I found this by timing a run and then profiling. Two early guesses (journal mode, where the file is stored) turned out not to matter. What helped was storing rows in arrival order with one unique index, a bigger cache and batched inserts, which brought it to about 90 seconds.
- The first draft of `write_reports` opened a transaction that `executescript` committed straight away, and it had a pointless `.replace()` call. This was tidied before the first real run.
- Setting up the virtual environment failed because the folder path was too long for Windows. It's not part of the solution, but it cost time.
- TODO: add anything you found yourself while reviewing.

## How I verified the code
- 90 pytest tests: rule-level tests for phones, dates, amounts and headers, hand-built cases for dispositions and buckets, and end-to-end checks on three generator seeds.
- The SQL reports are compared against a second, simple in-memory implementation (`solution/tests/reference.py`). Both came from the same AI and the same reading of the spec, so this finds bugs but not a misread rule.
- Byte-for-byte comparison of the reports: drop by drop against all at once, and before and after a re-ingest.
- A test that searches the raw database file for customer names and phone numbers.
- Manual runs on the small and large datasets. The README's example values (`0.5625`, `2026-10-14T17:55:24+05:30`) show up in the seed 42 output.
- TODO: describe your own review, for example reading each module or working out a few report rows by hand from the raw files.
