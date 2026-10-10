from copy import deepcopy
import hashlib
from io import BytesIO
from zipfile import ZipFile

import pytest

from scripts.trace_traffic_revisions import compare_quarter, match_payloads, segment_rows


def archive(lines):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as z:
        z.writestr("DD.example.asc", "\n".join(lines) + "\n")
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def line(year="2021", month="01", passengers="20", seats="30", freight="0"):
    return "|".join([year, month, "AAA", "12345", "91", "Airport A", "BBB", "12346", "74", "Airport B",
        "AA", "0A050", "3", "100", "F", "6", "614", "1", "2", "2", "1000", seats, passengers,
        freight, "0", "100", "90", "10"])


def rows(lines, periods):
    data, sha = archive(lines)
    return segment_rows(data, sha, "DD", periods)


def test_matching_ignores_only_date_fields_and_preserves_literal_payload():
    old = rows([line("2020")], {"2020-01"})
    new = rows([line()], {"2021-01"})
    assert match_payloads(old, new)["every_payload_matches"]
    changed = rows([line(freight="1")], {"2021-01"})
    assert match_payloads(old, changed)["matched_rows"] == 0
    assert changed[0]["text"] == line(freight="1")
    assert changed[0]["source_line"] == 1


def test_matching_retains_multiplicity_and_scope():
    old = rows([line("2020"), line("2020")], {"2020-01"})
    new = rows([line()], {"2021-01"})
    match = match_payloads(old, new)
    assert match["matched_rows"] == 1 and not match["every_payload_matches"]
    assert len(match["unmatched_earlier_row_ids"]) == 1
    different_scope = deepcopy(new)
    different_scope[0]["scope"] = "FD"
    assert match_payloads(old, different_scope)["matched_rows"] == 0
    assert not match_payloads([], [])["every_payload_matches"]


def test_source_checksum_and_locked_quantity_isolation():
    data, sha = archive([line("2024", passengers="invalid"), line()])
    assert len(segment_rows(data, sha, "DD", {"2021-01"})) == 1
    with pytest.raises(ValueError, match="locked"):
        segment_rows(data, sha, "DD", {"2024-01"})
    with pytest.raises(ValueError, match="checksum"):
        segment_rows(data + b"x", sha, "DD", {"2021-01"})


def quarter():
    return rows([line(month=m) for m in ("01", "02", "03")], {"2021-01", "2021-02", "2021-03"})


def witness(values=None):
    return {"grain": ["0A050", "6", "614", "1"], "source_line": 2,
        "values": values or {"rpm": "6000.00", "asm": "9000.00", "departures": "6.00"}}


def test_exact_reporting_grain_quarter_comparison_and_nonzero_residual():
    result = compare_quarter(quarter(), [witness()])
    assert result["exact_rpm_asm_departure_grains"] == 1
    result = compare_quarter(quarter(), [witness({"rpm": "6001", "asm": "9000", "departures": "6"})])
    assert result["grains"][0]["difference_segment_minus_t2"]["rpm"] == "-1"
    assert result["exact_rpm_asm_departure_grains"] == 0


def test_missing_t2_quantity_and_absent_grain_remain_unknown():
    result = compare_quarter(quarter(), [witness({"rpm": None, "asm": "9000", "departures": "6"})])
    assert result["grains"][0]["difference_segment_minus_t2"]["rpm"] is None
    result = compare_quarter(quarter(), [])
    assert result["grains"][0]["status"] == "missing_t2_grain"
    assert result["grains"][0]["difference_segment_minus_t2"]["asm"] is None


def test_quarter_requires_all_months_and_unique_t2_grains():
    with pytest.raises(ValueError, match="incomplete"):
        compare_quarter(quarter()[:1], [witness()])
    with pytest.raises(ValueError, match="Duplicate"):
        compare_quarter(quarter(), [witness(), witness()])
