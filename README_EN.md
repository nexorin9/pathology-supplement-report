# Pathology Supplemental Report Generation System

> Immunohistochemistry and molecular-lab supplemental results are dropped into the result inbox; the system matches every result back to its specimen site and paraffin-block number, and within about a minute writes a **supplemental report draft grouped by specimen site, with the source and report time of every item**, plus a separate list of the results that could not be matched.

## What it is / who it is for

`pathology-supplement-report` is a resident service (daemon + operations CLI) for hospital pathology departments. It watches the result drop directory read-only, parses inbound `ORU^R01` / `ORM^O01` HL7 v2 messages (the specimen site and block number travel in the in-house `ZBR` segment), aligns each reported result against the baseline site list fetched from the pathology information system, and writes the composed draft back to that system's **draft area**.

Intended users: pathologists on primary and review rotations who have to write a supplemental report once a batch of immunohistochemistry / molecular results comes back; pathology technicians who have to fill in a missing block number or site description; the immunohistochemistry and molecular laboratories that deliver the results; and the hospital IT department that maintains the interface and the credentials.

It produces **no interpretation and no diagnostic conclusion**, and it signs nothing: the draft goes to the draft area only, and the pathologist signs inside the hospital system as usual. Every result either enters the draft or lands on the manual-handling list — there is no third destination.

## Core capabilities

- **Results are matched back to site and block number** — the key is "accession number + site index + block number": exact match on the block number first, then on the site description; if neither matches, the system stops and waits for a human instead of guessing. Every line of the draft traces back to its source system and its report time.
- **Unmatched results are never forced into the draft** — a missing block number, an absent site description and several equal-score candidates are all suspended, with the missing field and the candidate list written out in the list (leaving one line out is a problem, writing one line wrong is an incident). Items already ordered but not yet reported take one "pending" line; results that came back but cannot be written into the report (unobtainable, empty value, unmatched) stay on the manual-handling list — never relabelled as "not yet reported" and never left as a blank line in the report.
- **Repeated delivery does not duplicate the report** — re-dropping the same batch keeps the item count unchanged; a corrected result replaces the old value and leaves one audit record instead of being appended as a second line. A correction whose report time is earlier than an existing result does **not** replace it: the newer original is kept and the correction is held for human confirmation.
- **The draft stops at the draft area; signing stays in the hospital system** — no interpretation, no proxy signature, no automatic signing. On a version conflict the write-back stops, keeps the local draft and leaves it to a human, never overwriting somebody else's version.
- **A temporary pathology-system outage loses no work** — failed write-backs enter a retry queue with increasing back-off; past the attempt limit the system escalates a notice, the draft state does not advance, and processing resumes after manual handling. Versions continue from the baseline (the baseline is version 3, so the supplemental report is version 4); old versions are appended, never overwritten, and can be looked up by version number at any time.
- **Notifications go to the person on duty, and a completed field resumes where it was suspended** — the department's own duty roster is used to find the current on-call physician and only that person is notified; if the roster is missing, expired or empty for the current period, delivery degrades to the department default recipients and the run summary says so (a notice to the whole department is a notice to nobody). A completion submits only "suspended-row ID + the missing field value" — the whole batch is never re-run.
- **Three-way benchmarking shows the alignment beats the usual in-house shortcuts** — on the same batch and the same metrics the product runs side by side with "normalisation + exact match only" and "highest lexical score only"; the benchmark exits non-zero when mis-binding or false merge is not zero, or when the alignment rate falls below a baseline, so it can be wired into CI.

## Quick Start

**Requirements** — Python ≥ 3.10 (deployed on 3.11+ on the hospital network); **zero third-party runtime dependencies** (standard library only, so the on-site machine needs no network install); Linux / macOS, run as a resident process under `systemd`.

**Install**

```bash
cd /opt/pathology-supplement-report
cp .env.example .env && vi .env                  # real values live in .env only, never in the repo
python3 -m pip install -r requirements.txt       # pytest only; not needed to run the daemon
```

**Configure** — configuration comes from two places: **environment variables take priority**, `.env` fills in the rest, and built-in defaults cover whatever is still missing. Credentials (API token, mail password) are read from environment variables only; no real address, port or secret appears in the code. The three mandatory settings are `PATHOLOGY_BASE_URL` (pathology system root URL), `PATHOLOGY_API_TOKEN` (outbound Bearer token, issued by the IT department on the pathology-system side) and `PATHOLOGY_RESULT_INBOX` (the drop directory the daemon watches read-only). Optional switches cover the state database, the department-maintained site-synonym lexicon, the duty roster, the SMTP / WeCom notification channels and the alignment thresholds. An out-of-range threshold (negative, non-numeric, out of range) is a **startup error**, not a silent fallback to the default.

**Run and verify**

```bash
python3 -c "import patho_supplement.config, patho_supplement.models, patho_supplement.db"  # import check
python3 -m patho_supplement.cli doctor    # the 5 interface contracts + credential slots (values never printed)
python3 -m pytest -q                       # test suite
python3 -m patho_supplement.cli replay --inbox tests/fixtures/inbox/ --out ./out
python3 -m patho_supplement.cli daemon     # resident: watch the inbox, write drafts back on each event
```

The daemon prints a structured line log and a per-run summary; there is no browser interface — the operating surface is the CLI plus the daemon log. Full command reference, configuration matrix, error and degradation table, output transcripts and measured screenshots: see [`docs/cli.md`](docs/cli.md), [`docs/examples.md`](docs/examples.md) and the Chinese [`README.md`](README.md).

## Repository layout

| Path | Contents |
|---|---|
| `src/patho_supplement/` | Package root — `ingest` (HL7 ER7 split, ORU / ORM parsing, `ZBR` segment, idempotency keys), `align` (lexicon, normalisation, multi-route retrieval, four-level matching, evidence, metrics), `pending` (snapshots, reason codes, mutex locks, resume), `compose` (draft rendering, version chain, state machine, outbound client, retry queue, conflict handling), `notify` (SMTP / WeCom channels, on-call routing), `audit`, plus `config.py` / `models.py` / `db.py` / `daemon.py` / `replay.py` / `recovery.py` / `scale.py` / `cli.py` |
| `dictionaries/` | Department-maintained external tables: site-synonym lexicon and duty-roster sample |
| `docs/` | CLI reference, output transcripts, inbound HL7 field spec, benchmark comparison, scale acceptance, visual target, measured screenshots |
| `tests/` | Unit, contract, three-way comparison and end-to-end tests, plus the de-identified sample corpus |
| `tools/` | Development helpers (not a runtime dependency): local pathology-system stub and screenshot renderer |
| `reports/` | Benchmark and scale acceptance output, distributed with the repository |

## License

MIT

Full documentation: see README.md (Chinese)

---

## Follow Us

Scan the QR code to follow our official account for updates and community access:

![Follow Us](qrcode.jpg)