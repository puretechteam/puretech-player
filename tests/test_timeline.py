# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
import os, time
import pytest
from puretech_player.timeline import Chunk, Source, Spot, parse_start, prefetch_at, PREFETCH_LEAD

T = time.mktime((2025, 3, 4, 14, 0, 0, 0, 0, -1))

def src(*spans, tolerance=1.5):
    """Source from (start offset, duration) pairs, no files needed."""
    return Source([Chunk(f"c{i}.mp4", T + s, d, order=i) for i, (s, d) in enumerate(spans)], gap_tolerance=tolerance)

def test_parse_day_folder_and_suffix():
    p = os.path.join("x", "2025-03-04", "14-02-08.mp4")
    assert parse_start(p) == (time.mktime((2025, 3, 4, 14, 2, 8, 0, 0, -1)), 0)
    assert parse_start(os.path.join("x", "2025-03-04", "14-02-08-2.mkv"))[1] == 2

def test_parse_stamp_in_name_and_unparsable():
    assert parse_start("rec 2025-03-04 14-02-08.webm")[0] == time.mktime((2025, 3, 4, 14, 2, 8, 0, 0, -1))
    assert parse_start(os.path.join("x", "holiday.mp4")) == (None, 0)
    assert parse_start(os.path.join("notaday", "14-02-08.mp4")) == (None, 0)

def test_from_files_sorts_by_wall_clock(tmp_path):
    day = tmp_path / "2025-03-04"
    day.mkdir()
    names = ["14-05-00.mp4", "14-00-00.mp4", "14-00-00-1.mp4"]
    for n in names:
        (day / n).write_bytes(b"")
    s = Source.from_folder(str(tmp_path))
    assert [os.path.basename(c.path) for c in s.chunks] == ["14-00-00.mp4", "14-00-00-1.mp4", "14-05-00.mp4"]
    assert s.start == T

def test_unnamed_files_follow_each_other():
    s = Source([Chunk("a.mp4", 0, 10), Chunk("b.mp4", 10, 5)])
    assert s.end == 15

def test_locate_inside_chunks():
    s = src((0, 60), (60, 60))
    assert s.locate(T + 30) == Spot(0, 30)
    assert s.locate(T + 60) == Spot(1, 0)
    assert s.locate(T + 119.5) == Spot(1, 59.5)

def test_locate_before_start_and_after_end():
    s = src((0, 60))
    assert s.locate(T - 0.5) == Spot(0, 0.0)                   # within tolerance: just the start
    assert s.locate(T - 30).gap == (T - 30, T)
    end = s.locate(T + 100)
    assert end.ended and end.index == 0

def test_small_hole_is_rounding_not_a_gap():
    s = src((0, 59.4), (60, 60))
    assert s.locate(T + 59.7) == Spot(1, 0.0)
    assert s.follow(0) == (1, 0.0, None)
    assert s.gaps() == []

def test_real_gap_is_reported():
    s = src((0, 60), (120, 60))
    spot = s.locate(T + 90)
    assert spot.gap == (T + 60, T + 120) and spot.index == 1 and spot.offset == 0
    assert s.follow(0) == (1, 0.0, (T + 60, T + 120))
    assert s.gaps() == [(T + 60, T + 120)]

def test_overlap_plays_each_moment_once():
    s = src((0, 61.2), (60, 60))
    assert s.locate(T + 60.5) == Spot(0, 60.5)                 # the earlier chunk still covers it
    i, off, gap = s.follow(0)
    assert (i, gap) == (1, None) and off == pytest.approx(1.2)

def test_unknown_length_runs_to_next_start():
    s = Source([Chunk("a.mp4", T, None), Chunk("b.mp4", T + 60, 60)])
    s[0]._info = {"duration": None, "start_time": 0, "video": None, "audio": None}
    assert s.end_of(0) == T + 60
    assert s.locate(T + 30) == Spot(0, 30)
    assert s.follow(0) == (1, 0.0, None)

def test_prefetch_plan_scales_with_speed():
    s = src((0, 60), (60, 60))
    assert prefetch_at(s, 0, 1.0) == T + 60 - PREFETCH_LEAD
    assert prefetch_at(s, 0, 8.0) == T + 60 - PREFETCH_LEAD * 8
    assert prefetch_at(s, 1) is None                           # last chunk
    short = src((0, 2), (2, 60))
    assert prefetch_at(short, 0, 4.0) == T                     # never before the chunk starts

def test_coerce_tuples_and_empty():
    s = Source.coerce([("b.mp4", T + 10, 5), ("a.mp4", T, 10)])
    assert [c.path for c in s.chunks] == ["a.mp4", "b.mp4"]
    with pytest.raises(ValueError):
        Source([])

def test_keyframe_before_uses_the_index_only_where_it_covers():
    c = Chunk("a.mp4", 0, 10.0)
    assert c.keyframe_before(1.5) is None            # index not read: never reads it here
    c._keys = [0.0, 1.0, 2.0, 3.0]
    assert c.keyframe_before(1.5) == 1.0
    assert c.keyframe_before(2.0) == 2.0 and c.keyframe_before(2.0 - 1e-9) == 2.0
    assert c.keyframe_before(3.9) == 3.0             # within one keyframe interval of the last one
    assert c.keyframe_before(4.5) is None            # past it: the file may have grown since it was read
    assert c.keyframe_before(-0.5) is None
    assert c.keyframes() == [0.0, 1.0, 2.0, 3.0]
    assert Chunk("b.mp4", 0, 10.0).keyframes() == []  # no ffprobe: unknown
