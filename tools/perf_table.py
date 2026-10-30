# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Markdown tables for the README from docs/perf.json (written by tools/measure.py).

    python tools/perf_table.py [docs/perf.json]"""

import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get(d, *keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d

def fmt(v, unit=""):
    return "n/a" if v is None else f"{v:g}{unit}"

def main(path):
    data = json.load(open(path, encoding="utf-8"))
    rows = [
        "| File | First frame (launch), ffplay / ours | Seek, ffplay / ours (exact frame) | CPU % of one core, "
        "ffplay / ours | Private RAM MB, ffplay / ours added | Dropped frames, ffplay / ours |",
        "|---|---|---|---|---|---|",
    ]
    for key, row in data["files"].items():
        f, o = row.get("ffplay", {}), row.get("ours", {})
        seek_f = f"{fmt(get(f, 'seek', 'median_ms'), ' ms')}"
        if get(f, "seek", "n") != get(f, "seek", "of"):
            seek_f += f" ({get(f, 'seek', 'n')} of {get(f, 'seek', 'of')} landed)"
        rows.append(" | ".join([
            f"| {key}",
            f"{fmt(get(f, 'first_frame', 'launch_to_picture_ms'))} / {fmt(get(o, 'first_frame', 'launch_to_picture_ms'), ' ms')}",
            f"{seek_f} / {fmt(get(o, 'seek', 'median_ms'), ' ms')} ({fmt(get(o, 'seek', 'exact_frame_median_ms'), ' ms')})"
            + (f" ({get(o, 'seek', 'n')} of {get(o, 'seek', 'of')} landed)" if get(o, "seek", "n") != get(o, "seek", "of") else ""),
            f"{fmt(get(f, 'steady', 'cpu_pct_of_one_core'))} / {fmt(get(o, 'steady', 'cpu_pct_of_one_core'))}",
            f"{fmt(get(f, 'steady', 'private_mb_mean'))} / {fmt(get(o, 'steady', 'added_private_mb_mean'))}",
            f"{fmt(get(f, 'steady', 'dropped'))} / {fmt(get(o, 'steady', 'dropped'))} |",
        ]))
    size = data["size"]
    rows += ["", f"Size: ffplay.exe {size['ffplay_exe_bytes'] / 2 ** 20:.1f} MB; puretech_player "
                 f"{size['package_py_bytes'] / 1024:.0f} KB of Python ({size['package_zip_bytes'] / 1024:.0f} KB zipped). "
                 f"{data['ffmpeg']}, {data['machine']['cpus']} logical CPUs, steady state over {data['seconds']} s at 1x."]
    print("\n".join(rows))

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "perf.json"))
