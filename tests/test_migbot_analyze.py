"""`migbot analyze`: grouping tokens by what was known at the start of the buy window."""
import os

from migbot.analyze import analyze, format_analysis
from migbot.cli import main
from migbot.storage import CsvJournal
from migbot.tracker import token_fields

CPS = [5, 15, 60]


def write_tokens(data_dir, rows):
    journal = CsvJournal(os.path.join(data_dir, "tokens.csv"), token_fields(CPS))
    for i, row in enumerate(rows):
        journal.append({"mint": f"M{i}", "status": "ditolak", "source": "pumpportal", **row})


def test_the_measure_that_separates_crashes_is_the_headline(tmp_path):
    rows = []
    for i in range(60):
        holders = 100 + i * 10  # 100 .. 690
        crashed = holders < 300  # the lowest third always crashes, the rest never
        rows.append({
            "holders": holders if i < 59 else "2000+",
            "top10_pct": 20 + (i % 3),  # unrelated to the outcome
            "ref_mcap_usd": 50_000 + (i % 5) * 10_000,
            "min_ret_pct": -95 if crashed else -30,
            "ret_60m": -90 if crashed else 10,
        })
    rows.append({"holders": 400, "min_ret_pct": "", "status": "tanpa data"})  # no market data: left out
    write_tokens(str(tmp_path), rows)
    result = analyze(str(tmp_path))
    assert result["n"] == 60 and round(result["overall"]["crash_pct"]) == 33
    holders = next(m for m in result["measures"] if m["label"] == "jumlah holder")
    assert [(g["label"], g["n"], round(g["crash_pct"])) for g in holders["groups"]] == [
        ("100–290", 20, 100), ("300–490", 20, 0), ("500–2000", 20, 0),
    ]
    assert holders["groups"][0]["median_60m"] == -90
    assert result["best"]["label"] == "jumlah holder" and round(result["best"]["spread"]) == 100
    text = format_analysis(result)
    assert "Paling membedakan: jumlah holder" in text
    assert "  100–290: hancur 100% (n=20)" in text
    assert "Baru 60 token" in text  # small sample warning
    assert max(len(line) for line in text.splitlines()) <= 60  # readable on a phone


def test_equal_values_stay_in_one_group(tmp_path):
    rows = [{"dev_pct": 0 if i < 40 else 3 + i % 4, "min_ret_pct": -90, "ret_60m": -80} for i in range(50)]
    write_tokens(str(tmp_path), rows)
    dev = next(m for m in analyze(str(tmp_path))["measures"] if m["label"] == "dev pegang")
    assert dev["groups"][0]["label"] == "0%" and dev["groups"][0]["n"] == 40
    assert sum(g["n"] for g in dev["groups"]) == 50


def test_no_data_and_cli(tmp_path, capsys):
    assert "Belum ada token" in format_analysis(analyze(str(tmp_path)))
    archive = tmp_path / "archive"
    write_tokens(str(archive), [{"holders": 300 + i, "min_ret_pct": -90 + i, "ret_60m": -50} for i in range(12)])
    assert main(["analyze", "--dir", str(archive)]) == 0
    assert "Semua: 12 token" in capsys.readouterr().out
    assert main(["analyze", "--dir", str(tmp_path / "missing")]) == 1
