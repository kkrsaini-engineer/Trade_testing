"""
2026-10-07 — Daily Scan push failure (GH001).

The real Daily Scan log (2026-10-05 run) showed the scan finished and
committed, then GitHub rejected the push: reports/full_report.csv was
102.11 MB, over the 100 MB per-file limit. Every run since 2026-09-22
started from the same 95.3 MiB file and added one ~7 MB scan, so every
push failed and candidates_order.json stayed at 2026-09-21.

Fix: full_report.csv keeps only the newest N scan dates; older dates move
unchanged into reports/archive/full_report_<date>.csv.gz. TradeID now
continues from the highest existing ID. The workflow unstages any file
over the limit (so the rest of the scan still lands) and stops retrying
on GH001.
"""

import csv
import gzip
from pathlib import Path

import yaml

from scripts.generate_full_report import FIELDNAMES, next_trade_id, rotate_full_report

HEADER = ["TradeID", "Date", "Stock", "Reason"]


def _write(path, rows, header=HEADER):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def _read(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", newline="") as f:
        reader = csv.reader(f)
        return next(reader), list(reader)


def _rows(dates, per_day=3):
    rows, tid = [], 1
    for d in dates:
        for i in range(per_day):
            rows.append([str(tid), d, f"S{i}.NS", f"reason {tid}"])
            tid += 1
    return rows


DATES = ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-10-07"]


def test_keeps_newest_dates_and_archives_the_rest(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    _write(report, _rows(DATES))

    archived = rotate_full_report(str(report), keep_scans=2, archive_dir=str(archive))

    assert archived == DATES[:4]
    header, kept = _read(report)
    assert header == HEADER
    assert sorted({r[1] for r in kept}) == DATES[-2:]
    assert sorted(p.name for p in archive.iterdir()) == [f"full_report_{d}.csv.gz" for d in DATES[:4]]


def test_no_row_is_lost_or_duplicated(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    original = _rows(DATES)
    _write(report, original)

    rotate_full_report(str(report), keep_scans=2, archive_dir=str(archive))

    combined = _read(report)[1]
    for gz in sorted(archive.iterdir()):
        header, rows = _read(gz)
        assert header == HEADER
        combined += rows
    assert sorted(combined) == sorted(original)


def test_multiline_and_comma_fields_survive(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    tricky = [["1", "2026-09-15", "A.NS", 'line one\nline two, "quoted"'], ["2", "2026-10-07", "B.NS", "x"]]
    _write(report, tricky)
    rotate_full_report(str(report), keep_scans=1, archive_dir=str(archive))
    assert _read(archive / "full_report_2026-09-15.csv.gz")[1] == [tricky[0]]


def test_nothing_happens_when_within_retention(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    _write(report, _rows(DATES[:2]))
    before = report.read_bytes()
    assert rotate_full_report(str(report), keep_scans=5, archive_dir=str(archive)) == []
    assert report.read_bytes() == before
    assert not archive.exists()


def test_rotation_is_idempotent(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    _write(report, _rows(DATES))
    rotate_full_report(str(report), keep_scans=2, archive_dir=str(archive))
    after_first = report.read_bytes()
    assert rotate_full_report(str(report), keep_scans=2, archive_dir=str(archive)) == []
    assert report.read_bytes() == after_first


def test_existing_archive_is_appended_without_a_second_header(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    archive.mkdir()
    with gzip.open(archive / "full_report_2026-09-15.csv.gz", "wt", newline="") as f:
        csv.writer(f).writerows([HEADER, ["0", "2026-09-15", "OLD.NS", "earlier"]])
    _write(report, _rows(["2026-09-15", "2026-10-07"]))

    rotate_full_report(str(report), keep_scans=1, archive_dir=str(archive))

    header, rows = _read(archive / "full_report_2026-09-15.csv.gz")
    assert header == HEADER
    assert [r[2] for r in rows] == ["OLD.NS", "S0.NS", "S1.NS", "S2.NS"]


def test_size_cap_keeps_fewer_dates_but_always_the_latest(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    _write(report, _rows(DATES, per_day=200))
    # ~1/1000 MB cap forces it down to the latest scan only.
    rotate_full_report(str(report), keep_scans=5, max_mb=0.001, archive_dir=str(archive))
    assert {r[1] for r in _read(report)[1]} == {DATES[-1]}


def test_next_trade_id_uses_highest_id_not_row_count(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    _write(report, _rows(DATES))                 # IDs 1..18
    rotate_full_report(str(report), keep_scans=1, archive_dir=str(archive))
    assert len(_read(report)[1]) == 3            # row count is now 3 ...
    assert next_trade_id(str(report)) == 19      # ... but IDs continue after 18


def test_next_trade_id_for_a_missing_file(tmp_path):
    assert next_trade_id(str(tmp_path / "nope.csv")) == 1


def test_works_with_the_real_report_header(tmp_path):
    report, archive = tmp_path / "full_report.csv", tmp_path / "archive"
    date_idx = FIELDNAMES.index("Date")
    rows = []
    for tid, d in enumerate(["2026-09-21", "2026-10-07"], start=1):
        row = [""] * len(FIELDNAMES)
        row[0], row[date_idx] = str(tid), d
        rows.append(row)
    _write(report, rows, header=FIELDNAMES)
    assert rotate_full_report(str(report), keep_scans=1, archive_dir=str(archive)) == ["2026-09-21"]


def test_main_rotates_after_writing_and_ids_continue():
    source = Path("scripts/generate_full_report.py").read_text()
    assert "next_id = next_trade_id(out_path)" in source
    assert "sum(1 for _ in csv.DictReader(f)) + 1" not in source
    assert source.index("writer.writerows(rows)") < source.index("archived_dates = rotate_full_report(out_path)")


def test_workflow_unstages_oversized_files_and_stops_retrying_on_gh001():
    workflow = yaml.safe_load(Path(".github/workflows/daily_scan.yml").read_text())
    commit = next(s for s in workflow["jobs"]["scan"]["steps"] if s.get("name") == "Commit updated report back to repo")
    script = commit["run"]
    assert '-gt 99000000' in script and 'git reset -q -- "$f"' in script
    assert script.index("git reset -q") < script.index("git commit")
    assert 'grep -q "GH001"' in script
