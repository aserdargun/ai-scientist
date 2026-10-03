"""CPU-only private source capture contracts; no live database acceptance claimed."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from lab.operating_modes import stream


def source(**changes):
    return {
        "source_id": "site:plant/telemetry",
        "source_version": "trusted revision 2026.10+1",
        "entity": None,
        "sensors": ["temperature", "pressure"],
        "units": ["degC", "bar"],
        "sampling_seconds": 2.5,
        "source_definition_sha256": "a" * 64,
        "local_export_allowed": False,
        **changes,
    }


def request(**changes):
    return {
        "source_id": source()["source_id"],
        "sensors": source()["sensors"],
        "start_utc": "2026-10-02T00:00:00+00:00",
        "end_utc": "2026-10-03T00:00:00+00:00",
        "entity": None,
        "row_limit": 23,
        "train_rows": 16,
        "chunk_rows": 3,
        **changes,
    }


def observed(count=23, *, irregular=True):
    ticks = pd.Timestamp("2026-10-02T00:00:00.000000123+00:00").value
    rows = []
    for index in range(count):
        if index:
            extra = {5: 0.125, 16: 10, 19: 7}.get(index, 0) if irregular else 0
            ticks += int((2.5 + extra) * 10**9)
        stamp = pd.Timestamp(ticks, unit="ns", tz="UTC")
        rows.append((stamp.isoformat(), (float(index), None if index == 17 else index / 10)))
    return rows


def capture(root, *, definition=None, selection=None, rows=None, **kwargs):
    return stream.create_database_input(
        root,
        source_definition=source() if definition is None else definition,
        request=request() if selection is None else selection,
        rows=iter(observed() if rows is None else rows),
        **kwargs,
    )


def test_actual_fractional_timeline_is_retained_and_bound_to_safe_source_summary(tmp_path):
    rows = observed()
    manifest = capture(tmp_path)
    assert isinstance(manifest, stream.DatabaseStreamManifest)
    assert manifest.schema_version == "mode-stream-input.v2"
    assert manifest.source_kind == "private_database"
    assert manifest.context_sampling_seconds is None
    assert manifest.sampling_seconds == 2.5
    assert manifest.gap_count == 3
    assert (
        manifest.timeline_sha256
        == hashlib.sha256(stream.canonical_json([stamp for stamp, _ in rows])).hexdigest()
    )
    assert manifest.request_sha256 == hashlib.sha256(stream.canonical_json(request())).hexdigest()
    assert [ref.rows for ref in manifest.chunks] == [3, 3, 1]
    assert [ref.row_offset for ref in manifest.chunks] == [0, 3, 6]
    assert manifest.source_summary == {
        "source_id": source()["source_id"],
        "source_version": source()["source_version"],
        "entity": None,
        "units": ["degC", "bar"],
        "first_utc": rows[0][0],
        "train_end_utc": rows[15][0],
        "last_utc": rows[-1][0],
        "sampling_seconds": 2.5,
        "timeline_sha256": manifest.timeline_sha256,
        "source_definition_sha256": "a" * 64,
        "gap_count": 3,
        "alarm_basis": "observed_rows",
        "local_export_allowed": False,
    }
    assert "source_kind" not in manifest.to_dict()  # Computed semantics, no v1 field addition.
    assert capture(tmp_path) == manifest == stream.load_input(tmp_path, manifest.sha256)
    frames = [stream.load_train(tmp_path, manifest)]
    for index, ref in enumerate(manifest.chunks):
        stamps, gaps = stream.read_chunk_timeline(tmp_path, manifest, index)
        assert stamps == [
            row[0] for row in rows[16 + ref.row_offset : 16 + ref.row_offset + ref.rows]
        ]
        assert gaps == ([True, False, False] if index < 2 else [False])
        frames.append(stream.read_chunk(tmp_path, manifest, index))
        raw = json.loads((tmp_path / manifest.sha256 / ref.name).read_bytes())
        assert raw["schema"] == "mode-stream-frame.v2"
        assert raw["timestamps_utc"] == stamps
    all_values = pd.concat(frames, ignore_index=True)
    pd.testing.assert_frame_equal(
        all_values,
        pd.DataFrame([row[1] for row in rows], columns=source()["sensors"], dtype=float),
    )
    assert list(all_values.columns) == source()["sensors"]


@pytest.mark.parametrize("sampling", [None, 1.0, 2.5])
def test_context_cadence_is_conservative_and_unknown_gaps_are_not_guessed(tmp_path, sampling):
    manifest = capture(tmp_path, definition=source(sampling_seconds=sampling))
    assert manifest.context_sampling_seconds is None
    assert manifest.sampling_seconds == sampling
    _, gaps = stream.read_chunk_timeline(tmp_path, manifest, 0)
    if sampling is None:
        assert manifest.gap_count is None
        assert gaps == [False, False, False]
    else:
        assert manifest.gap_count is not None


def test_datetime_and_z_timestamps_are_canonicalized_without_retiming(tmp_path):
    rows = observed(irregular=False)
    rows[0] = (datetime(2026, 10, 2, tzinfo=UTC), rows[0][1])
    rows[1] = (rows[1][0].replace("+00:00", "Z"), rows[1][1])
    manifest = capture(tmp_path, rows=rows)
    assert manifest.train.start_utc == "2026-10-02T00:00:00+00:00"
    payload = json.loads((tmp_path / manifest.sha256 / "train.json").read_bytes())
    assert payload["timestamps_utc"][1] == "2026-10-02T00:00:02.500000123+00:00"


def test_legacy_unknown_units_remain_an_explicit_empty_list(tmp_path):
    manifest = capture(tmp_path, definition=source(units=[]))
    assert manifest.units == ()
    assert manifest.source_summary["units"] == []
    assert stream.load_input(tmp_path, manifest.sha256) == manifest
    assert stream.read_chunk_timeline(tmp_path, manifest, 0)[1] == [True, False, False]


@pytest.mark.parametrize("index", [2, 16, 19])
@pytest.mark.parametrize("backward", [False, True])
def test_duplicates_and_reversals_fail_within_train_and_across_all_boundaries(
    tmp_path, index, backward
):
    rows = observed()
    rows[index] = (rows[index - (2 if backward else 1)][0], rows[index][1])
    with pytest.raises(ValueError, match="strictly increasing"):
        capture(tmp_path, rows=rows)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-10-02T00:00:00",  # Naive timestamps have no authorized UTC identity.
        "2026-10-02T00:00:00+03:00",
        "not-a-timestamp",
        "NaT",
        12,
        None,
        "2026-10-01T23:59:59.999999999+00:00",
        "2026-10-03T00:00:00+00:00",  # The end of the selection is exclusive.
        "2026-10-02T00:00:00.1234567891+00:00",  # Excess precision cannot be truncated.
    ],
)
def test_invalid_or_out_of_window_timestamp_never_publishes(tmp_path, stamp):
    rows = observed()
    rows[0] = (stamp, rows[0][1])
    with pytest.raises(ValueError):
        capture(tmp_path, rows=rows)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("item", [True, "1.5", {}, float("nan"), float("inf"), 10**400])
def test_values_are_finite_numeric_or_null_only(tmp_path, item):
    rows = observed()
    rows[19] = (rows[19][0], (item, 1.0))
    with pytest.raises(ValueError):
        capture(tmp_path, rows=rows)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "definition,selection",
    [
        (source(sensors=["label", "pressure"]), request(sensors=["label", "pressure"])),
        (source(sensors=["timestamp", "pressure"]), request(sensors=["timestamp", "pressure"])),
        (source(units=["degC"]), request()),
        (source(source_definition_sha256="A" * 64), request()),
        (source(source_version=""), request()),
        (source(local_export_allowed=1), request()),
        (source(sampling_seconds=True), request()),
        (source(sampling_seconds=float("inf")), request()),
        (source(sampling_seconds=0), request()),
        (source(), request(source_id="other")),
        (source(), request(entity="other")),
        (source(), request(row_limit=65537)),
        (source(), request(train_rows=15)),
        (source(), request(train_rows=4097)),
        (source(), request(train_rows=23)),
        (source(), request(chunk_rows=65)),
        (source(), request(chunk_rows=0)),
        (source(), request(row_limit=True)),
        (source(), request(idempotency_key="must-remain-outside-the-recipe")),
    ],
)
def test_invalid_definition_and_request_fail_before_consuming_source(
    tmp_path, definition, selection
):
    def untouched():
        pytest.fail("invalid selection consumed database rows")
        yield

    with pytest.raises(ValueError):
        capture(tmp_path, definition=definition, selection=selection, rows=untouched())
    assert list(tmp_path.iterdir()) == []


def test_row_limit_plus_one_is_rejected_without_consuming_unbounded_source(tmp_path):
    consumed = []

    def endless():
        for index, row in enumerate(observed(100)):
            consumed.append(index)
            yield row

    with pytest.raises(ValueError, match="row limit"):
        capture(tmp_path, rows=endless())
    assert len(consumed) == request()["row_limit"] + 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("count", [0, 15, 16])
def test_actual_training_and_at_least_one_monitor_row_are_required(tmp_path, count):
    with pytest.raises(ValueError, match="training and monitor"):
        capture(tmp_path, rows=observed(count))
    assert list(tmp_path.iterdir()) == []


def test_short_capture_is_valid_with_one_actual_monitor_row(tmp_path):
    manifest = capture(tmp_path, rows=observed(17))
    assert [ref.rows for ref in manifest.chunks] == [1]
    assert manifest.request.row_limit == 23


def test_total_row_limit_includes_training_and_each_chunk_stays_bounded(tmp_path):
    count = 65536
    selection = request(row_limit=count, chunk_rows=64, end_utc="2026-10-05T00:00:00+00:00")
    base = pd.Timestamp(selection["start_utc"])

    def bounded_rows():
        for index in range(count):
            yield (
                pd.Timestamp(base.value + index * 2_500_000_000, unit="ns", tz="UTC"),
                (
                    float(index),
                    None,
                ),
            )

    manifest = capture(tmp_path, selection=selection, rows=bounded_rows())
    assert manifest.train.rows + sum(ref.rows for ref in manifest.chunks) == count
    assert len(manifest.chunks) == 1024
    assert max(ref.rows for ref in manifest.chunks) == 64
    assert manifest.chunks[-1].rows == 48
    assert max(ref.size_bytes for ref in manifest.chunks) <= stream.MAX_CHUNK_BYTES
    assert manifest.train.size_bytes <= stream.MAX_TRAIN_BYTES
    assert manifest.gap_count == 0


def test_more_than_1024_actual_chunks_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="1024 chunks"):
        capture(
            tmp_path,
            selection=request(row_limit=1041, chunk_rows=1),
            rows=observed(1041),
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "bound", ["MAX_TRAIN_BYTES", "MAX_CHUNK_BYTES", "MAX_INPUT_TOTAL_BYTES", "MAX_MANIFEST_BYTES"]
)
def test_each_encoded_byte_bound_is_checked_before_publication(tmp_path, monkeypatch, bound):
    monkeypatch.setattr(stream, bound, 1)
    with pytest.raises(ValueError, match="byte"):
        capture(tmp_path)
    assert list(tmp_path.iterdir()) == []


def rehash_changed_input(root, manifest, change):
    directory = root / manifest.sha256
    value = manifest.to_dict()
    change(directory, value)
    value.pop("sha256")
    value["sha256"] = hashlib.sha256(stream.canonical_json(value)).hexdigest()
    path = directory / "manifest.json"
    path.chmod(0o600)
    path.write_bytes(stream.canonical_json(value))
    directory.rename(root / value["sha256"])
    return value["sha256"]


@pytest.mark.parametrize("change_kind", ["timeline", "gap", "duplicate", "version", "units"])
def test_rehashed_tampering_is_rejected_by_actual_frame_or_full_timeline_validation(
    tmp_path, change_kind
):
    manifest = capture(tmp_path)

    def change(directory, value):
        if change_kind == "gap":
            value["gap_count"] += 1
            return
        ref = value["chunks"][0]
        path = directory / ref["name"]
        frame = json.loads(path.read_bytes())
        match change_kind:
            case "timeline":
                frame["timestamps_utc"][1] = pd.Timestamp(
                    pd.Timestamp(frame["timestamps_utc"][1]).value + 100_000_000,
                    unit="ns",
                    tz="UTC",
                ).isoformat()
            case "duplicate":
                frame["timestamps_utc"][1] = frame["timestamps_utc"][0]
            case "version":
                frame["schema"] = "mode-stream-frame.v1"
            case "units":
                frame["units"] = ["wrong", "units"]
        payload = stream.canonical_json(frame)
        path.chmod(0o600)
        path.write_bytes(payload)
        ref.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))

    changed_sha = rehash_changed_input(tmp_path, manifest, change)
    with pytest.raises(ValueError, match="timeline|increasing|identity"):
        stream.load_input(tmp_path, changed_sha)


@pytest.mark.parametrize("index", [0, 1])
def test_chunk_timeline_verifies_actual_preceding_frame_including_train(tmp_path, index):
    manifest = capture(tmp_path)
    prior = manifest.train if index == 0 else manifest.chunks[index - 1]
    path = tmp_path / manifest.sha256 / prior.name
    path.chmod(0o600)
    path.write_bytes(path.read_bytes().replace(b"temperature", b"wrong_sensor"))
    # The numeric per-step reader remains bounded to the requested chunk.
    assert len(stream.read_chunk(tmp_path, manifest, index)) == 3
    with pytest.raises(ValueError, match="hash|byte bound"):
        stream.read_chunk_timeline(tmp_path, manifest, index)


@pytest.mark.parametrize("point", ["row", "manifest", "validation"])
def test_cancellation_before_publication_cleans_only_own_temporary_input(
    tmp_path, monkeypatch, point
):
    sentinel = tmp_path / "another-capture"
    sentinel.mkdir()
    (sentinel / "keep").write_text("unrelated")
    blocked = False
    original_write, original_frame = stream._write, stream._database_frame

    def write(path, payload):
        nonlocal blocked
        original_write(path, payload)
        if point == "manifest" and path.name == "manifest.json":
            blocked = True

    def frame(directory, manifest, ref, check):
        nonlocal blocked
        result = original_frame(directory, manifest, ref, check)
        if point == "validation" and ref == manifest.chunks[-1]:
            blocked = True
        return result

    def rows():
        nonlocal blocked
        for index, row in enumerate(observed()):
            if point == "row" and index == 19:
                blocked = True
            yield row

    def admitted():
        if blocked:
            raise RuntimeError("capture stopped or original deadline expired")

    monkeypatch.setattr(stream, "_write", write)
    monkeypatch.setattr(stream, "_database_frame", frame)
    with pytest.raises(RuntimeError, match="original deadline"):
        capture(tmp_path, rows=rows(), admission_check=admitted)
    assert list(tmp_path.iterdir()) == [sentinel]
    assert (sentinel / "keep").read_text() == "unrelated"


def test_load_input_checks_original_admission_after_each_read(tmp_path, monkeypatch):
    manifest = capture(tmp_path)
    original = stream._read
    reads = []

    def read(path, *args):
        reads.append(path.name)
        return original(path, *args)

    def admitted():
        if "chunk-00000.json" in reads:
            raise RuntimeError("capture deadline expired")

    monkeypatch.setattr(stream, "_read", read)
    with pytest.raises(RuntimeError, match="deadline"):
        stream.load_input(tmp_path, manifest.sha256, admission_check=admitted)
    assert reads == ["manifest.json", "train.json", "chunk-00000.json"]


def test_v1_canonical_manifest_and_frame_hashes_are_unchanged(tmp_path):
    manifest = stream.create_synthetic_input(
        tmp_path,
        stream.SyntheticStreamRequest(
            scenario="healthy_single", seed=7, train_rows=32, evaluation_rows=130, chunk_rows=64
        ),
    )
    assert manifest.sha256 == "86333128cbd573131d6530fa573e77572983454e92e048700a31ac7615f9e2eb"
    assert (
        manifest.train.sha256 == "42653487fbc0f15398f44ae04eaec1d5c067346f74b324bd82b6c4ae3ac4d338"
    )
    assert (
        manifest.chunks[0].sha256
        == "4cb55d37050266f0312f8c40ef5a8dfdd1fbbb675a8b9ccc9243cecdb4270703"
    )
    assert hashlib.sha256(stream.canonical_json(manifest.to_dict())).hexdigest() == (
        "ecd58ee9214cf114499ae32414ad3e0443d8b4a157f15c5ddab6cbdf3a06607d"
    )
    assert manifest.source_summary is None
    assert manifest.context_sampling_seconds == 1
    stamps, gaps = stream.read_chunk_timeline(tmp_path, manifest, 0)
    assert stamps[0] == manifest.chunks[0].start_utc
    assert gaps == [False] * 64
