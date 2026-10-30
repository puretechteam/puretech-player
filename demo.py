# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Open a folder of chunks (or files) and play them as one timeline.

    python demo.py <folder or files...> [--ffmpeg PATH] [--speed 1] [--volume 1] [--play] [--exit-after S]

Keys: Space play/pause, Left/Right 5 s back/forward, comma/period one frame back/forward, [ and ] slower/faster,
I and O set the clip's in and out, E export the clip next to the first chunk, G skip a gap."""

import argparse, ctypes, json, os, queue, sys, time
import tkinter as tk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from puretech_player import Player, VideoView, fmt_wall, plan_clip, export_clip, warm_up
from puretech_player.policy import step_speed

def main(argv=None):
    ap = argparse.ArgumentParser(description="Play chunked recordings as one timeline.")
    ap.add_argument("paths", nargs="+", help="a folder of chunks (day folders allowed) or media files")
    ap.add_argument("--ffmpeg", help="ffmpeg.exe to use (default: PATH)")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--volume", type=float, default=1.0)
    ap.add_argument("--play", action="store_true", help="start playing at once")
    ap.add_argument("--size", default="1280x720", help="window size, WxH")
    ap.add_argument("--exit-after", type=float, help="close after this many seconds of playing, printing stats")
    args = ap.parse_args(argv)
    warm_up()                       # Direct3D starts while Tk and the window come up
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
    paths = args.paths[0] if len(args.paths) == 1 else args.paths

    root = tk.Tk()
    root.title("Pure Tech Player")
    root.configure(background="black")
    w, h = (int(x) for x in args.size.split("x"))
    view = VideoView(root, width=w, height=h)
    view.pack(fill="both", expand=True)
    bar = tk.Frame(root, background="#202020")
    bar.pack(fill="x")
    status = tk.StringVar(value="Loading")
    clock = tk.StringVar(value="--:--:--")
    events = queue.Queue()

    player = Player(paths, ffmpeg=args.ffmpeg, view=view, volume=args.volume)
    player.on_state = lambda state, info: events.put((state, info))
    src = player.source
    root.title(f"Pure Tech Player - {src.name or os.path.basename(src[0].path)}")
    marks = {"in": None, "out": None}
    span = max(1.0, src.end_of(len(src) - 1) - src.start)
    scrub = tk.Scale(bar, from_=0, to=span, orient="horizontal", resolution=0.1, showvalue=False,
                     background="#202020", troughcolor="#404040", highlightthickness=0, borderwidth=0)
    dragging = {"on": False}
    scrub.bind("<ButtonPress-1>", lambda e: dragging.update(on=True))
    scrub.bind("<ButtonRelease-1>", lambda e: (dragging.update(on=False), player.seek(src.start + scrub.get())))

    def button(text, cmd):
        tk.Button(bar, text=text, command=cmd, background="#303030", foreground="white", relief="flat",
                  activebackground="#505050", activeforeground="white", padx=8).pack(side="left", padx=2, pady=4)

    def speed(direction):
        player.set_speed(step_speed(player.speed if direction else 1.0, direction))
        root.after(50, lambda: status.set(f"{player.speed:g}x"))

    def mark(which):
        marks[which] = player.position
        status.set(f"{'In' if which == 'in' else 'Out'} at {fmt_wall(marks[which])}")

    def export():
        if marks["in"] is None or marks["out"] is None:
            status.set("Set an in (I) and out (O) point first")
            return
        plan = plan_clip(src, marks["in"], marks["out"], player.ffprobe)
        status.set(plan.describe())
        root.update_idletasks()
        first = src[0].path
        out = os.path.join(os.path.dirname(first), f"clip {fmt_wall(plan.start).replace(':', '-')}{plan.container}")
        files = export_clip(plan, out, player.ffmpeg)
        status.set(f"{plan.describe()}: saved {', '.join(os.path.basename(f) for f in files)}")

    button("Play/Pause", player.toggle)
    button("< Frame", lambda: player.step(-1))
    button("Frame >", lambda: player.step(1))
    button("Slower", lambda: speed(-1))
    button("1x", lambda: speed(0))
    button("Faster", lambda: speed(1))
    button("In", lambda: mark("in"))
    button("Out", lambda: mark("out"))
    button("Export clip", export)
    button("Skip gap", player.skip_gap)
    tk.Label(bar, textvariable=clock, background="#202020", foreground="white", font=("Segoe UI", 11, "bold"),
             padx=8).pack(side="left")
    tk.Label(bar, textvariable=status, background="#202020", foreground="#c0c0c0", padx=8).pack(side="left")
    scrub.pack(side="left", fill="x", expand=True, padx=8)

    keys = {"space": player.toggle, "Left": lambda: player.seek(player.position - 5),
            "Right": lambda: player.seek(player.position + 5), "comma": lambda: player.step(-1),
            "period": lambda: player.step(1), "bracketleft": lambda: speed(-1), "bracketright": lambda: speed(1),
            "i": lambda: mark("in"), "o": lambda: mark("out"), "e": export, "g": player.skip_gap}
    for key, fn in keys.items():
        root.bind(f"<{key}>", lambda e, fn=fn: fn())

    started = {"t": None}

    def poll():
        while True:
            try:
                state, info = events.get_nowait()
            except queue.Empty:
                break
            if state == "gap":
                status.set(f"No recording {fmt_wall(info['start'])} to {fmt_wall(info['end'])}"
                           + (" (skipped)" if info.get("skipped") else ": G skips"))
            elif state not in ("loading",):
                status.set(f"{state.capitalize()} {player.speed:g}x")
            if state == "playing" and started["t"] is None:
                started["t"] = time.monotonic()
        pos = player.position
        clock.set(fmt_wall(pos))
        if not dragging["on"]:
            scrub.set(pos - src.start)
        if args.exit_after and started["t"] and time.monotonic() - started["t"] >= args.exit_after:
            stats = dict(player.stats)
            if player._audio:
                stats["audio_underruns"] = player._audio.underruns
                stats["audio_failed"] = player._audio.failed
            print(json.dumps(stats), flush=True)
            player.close()
            root.destroy()
            return
        root.after(100, poll)

    def closing():
        player.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", closing)
    if args.speed != 1.0:
        player.set_speed(args.speed)
    if args.play:
        player.play()
    root.after(100, poll)
    root.mainloop()
    player.close()

if __name__ == "__main__":
    main()
