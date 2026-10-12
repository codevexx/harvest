"""Cars learn to drive a race track by evolution (neuroevolution). Harvest-ready.

Every car has 5 distance sensors (how far to the edge of the road, ahead and to
the sides) plus its speed, and a tiny neural network that turns those into
steering and throttle. A generation of cars drives at once; the ones that get
furthest round the track are copied with small random changes into the next
generation. After a few dozen generations they lap the track.

On Harvest:
  * One chart point per generation: laps the best car drove in a 40-second race
    (it keeps climbing as the cars learn to go faster, brake and take racing lines).
  * Saves into outputs/: GIFs of the best cars as they improve, best.gif,
    replay.html (open it in a browser to watch the final generation race) and
    best_car.json (the winning brain).
  * Pauses for games and continues where it stopped, on any of your machines
    (checkpoint.npz in outputs/).
  * Sweep-friendly: try "mutation = 0.05, 0.15, 0.3" or "hidden = 4, 8, 16".

Run locally:  python car_race.py --generations 60
"""
import argparse
import json
import math
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT = os.environ.get("HARVEST_OUTPUTS", "outputs")
W, H, CELL = 800, 520, 2            # world size in pixels, collision-grid cell size
HALF_WIDTH = 23                     # half the road width
RAYS = np.radians([-70, -35, 0, 35, 70])
RAY_STEPS = np.linspace(4, 160, 40)  # how far the sensors see
VMAX, ACC, DRAG, TURN, BRAKE = 9.0, 0.4, 0.04, 0.11, 0.6
RACE_TICKS = 1200                   # 40 seconds at 30 frames a second


def say(obj):
    print(json.dumps(obj), flush=True)


# --- the track ----------------------------------------------------------------

class Track:
    def __init__(self, seed):
        rng = np.random.default_rng(seed)
        th = np.linspace(0, 2 * np.pi, 360, endpoint=False)
        r = 1.0
        for k, lo, hi in ((2, .10, .20), (3, .10, .18), (4, .06, .12), (6, .03, .07)):
            r = r + rng.uniform(lo, hi) * np.sin(k * th + rng.uniform(0, 2 * np.pi))
        pts = np.stack([r * np.cos(th), r * np.sin(th)], 1)
        m = HALF_WIDTH + 25  # fit inside the world with a margin
        for a, size in ((0, W), (1, H)):
            lo, hi = pts[:, a].min(), pts[:, a].max()
            pts[:, a] = m + (pts[:, a] - lo) / (hi - lo) * (size - 2 * m)
        self.pts = pts
        seg = np.roll(pts, -1, 0) - pts
        seg_len = np.hypot(seg[:, 0], seg[:, 1])
        self.arc0 = np.concatenate([[0], np.cumsum(seg_len)[:-1]])
        self.length = float(seg_len.sum())
        # Precompute, for every grid cell: on the road? and how far along the lap.
        gy, gx = np.mgrid[0:H // CELL, 0:W // CELL]
        q = np.stack([gx.ravel(), gy.ravel()], 1) * CELL + CELL / 2
        on = np.zeros(len(q), bool)
        arc = np.zeros(len(q))
        for i in range(0, len(q), 4000):
            c = q[i:i + 4000, None, :] - pts[None]
            t = np.clip((c * seg[None]).sum(2) / seg_len ** 2, 0, 1)
            d = np.hypot(*(c - t[..., None] * seg[None]).transpose(2, 0, 1))
            j = d.argmin(1)
            on[i:i + 4000] = d[np.arange(len(j)), j] < HALF_WIDTH
            arc[i:i + 4000] = self.arc0[j] + t[np.arange(len(j)), j] * seg_len[j]
        self.on = on.reshape(gy.shape)
        self.arc = arc.reshape(gy.shape)
        self.start = pts[0]
        self.start_heading = math.atan2(seg[0, 1], seg[0, 0])

    def lookup(self, grid, xy, outside):
        ix = (xy[..., 0] // CELL).astype(int)
        iy = (xy[..., 1] // CELL).astype(int)
        inside = (ix >= 0) & (iy >= 0) & (ix < grid.shape[1]) & (iy < grid.shape[0])
        out = np.full(ix.shape, outside, dtype=grid.dtype)
        out[inside] = grid[iy[inside], ix[inside]]
        return out


# --- the cars' brains ------------------------------------------------------------

def genome_size(hidden):
    n_in = len(RAYS) + 1
    return n_in * hidden + hidden + hidden * 2 + 2


def unpack(pop, hidden):
    n_in, P = len(RAYS) + 1, len(pop)
    a = n_in * hidden
    w1 = pop[:, :a].reshape(P, n_in, hidden)
    b1 = pop[:, a:a + hidden]
    w2 = pop[:, a + hidden:a + hidden + hidden * 2].reshape(P, hidden, 2)
    b2 = pop[:, -2:]
    return w1, b1, w2, b2


# --- one race ----------------------------------------------------------------------

def race(track, pop, hidden, record=False):
    """Drive every car for RACE_TICKS. Returns laps driven (fitness) and, if
    record, each car's path [(x, y, heading), ...]."""
    P = len(pop)
    w1, b1, w2, b2 = unpack(pop, hidden)
    pos = np.tile(track.start, (P, 1)).astype(float)
    head = np.full(P, track.start_heading)
    v = np.full(P, 1.0)
    alive = np.ones(P, bool)
    progress = np.zeros(P)
    best_prog = np.zeros(P)
    last_gain = np.zeros(P, int)
    arc = track.lookup(track.arc, pos, 0.0)
    paths = [[] for _ in range(P)] if record else None
    for t in range(RACE_TICKS):
        if not alive.any():
            break
        ang = head[:, None] + RAYS[None]
        dirs = np.stack([np.cos(ang), np.sin(ang)], -1)
        pts = pos[:, None, None, :] + RAY_STEPS[None, None, :, None] * dirs[:, :, None, :]
        hit = ~track.lookup(track.on, pts, False)
        first = np.where(hit.any(2), RAY_STEPS[hit.argmax(2)], RAY_STEPS[-1])
        x = np.concatenate([first / RAY_STEPS[-1], (v / VMAX)[:, None]], 1)
        h1 = np.tanh(np.einsum("pi,pih->ph", x, w1) + b1)
        out = np.tanh(np.einsum("ph,pho->po", h1, w2) + b2)
        steer, pedal = out[:, 0], out[:, 1]  # pedal > 0 accelerates, < 0 brakes
        a = alive
        push = np.where(pedal > 0, pedal * ACC, pedal * BRAKE)
        v[a] = np.clip(v[a] + push[a] - DRAG * v[a], 0.3, VMAX)
        # Less grip at speed: fast cars must slow down for tight corners.
        head[a] += steer[a] * TURN * np.minimum(1, v[a] / 2) * (1 - 0.55 * v[a] / VMAX)
        pos[a] += v[a, None] * np.stack([np.cos(head[a]), np.sin(head[a])], 1)
        new_arc = track.lookup(track.arc, pos, 0.0)
        d = new_arc - arc
        d = np.where(d < -track.length / 2, d + track.length, np.where(d > track.length / 2, d - track.length, d))
        progress[a] += d[a]
        arc = new_arc
        gained = progress > best_prog + 2
        best_prog = np.maximum(best_prog, progress)
        last_gain[gained] = t
        crashed = a & ~track.lookup(track.on, pos, False)
        stalled = a & ((t - last_gain > 120) | (progress < -40))
        alive &= ~(crashed | stalled)
        if record:
            for i in range(P):
                paths[i].append((float(pos[i, 0]), float(pos[i, 1]), float(head[i])))
    return best_prog / track.length, paths


# --- pictures -------------------------------------------------------------------------

def track_image(track):
    img = Image.new("RGB", (W, H), (46, 94, 50))
    d = ImageDraw.Draw(img)
    for r, col in ((HALF_WIDTH + 3, (235, 235, 235)), (HALF_WIDTH, (70, 72, 76))):  # kerb, then asphalt
        for x, y in track.pts:
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    for i in range(0, len(track.pts), 10):  # dashed centre line
        d.line([tuple(track.pts[i]), tuple(track.pts[(i + 4) % len(track.pts)])], fill=(190, 190, 190), width=2)
    sx, sy = track.start
    nx, ny = -math.sin(track.start_heading), math.cos(track.start_heading)
    d.line([(sx - nx * HALF_WIDTH, sy - ny * HALF_WIDTH), (sx + nx * HALF_WIDTH, sy + ny * HALF_WIDTH)], fill=(255, 255, 255), width=4)
    return img


def car_shape(x, y, h, size=9):
    c, s = math.cos(h), math.sin(h)
    pts = [(size, 0), (-size * .7, size * .55), (-size * .4, 0), (-size * .7, -size * .55)]
    return [(x + px * c - py * s, y + px * s + py * c) for px, py in pts]


try:
    FONT = ImageFont.load_default(size=18)
except TypeError:  # Pillow older than 10.1
    FONT = ImageFont.load_default()


def save_gif(track, paths, fits, title, path):
    base = track_image(track)
    order = np.argsort(-fits)
    show = [paths[i] for i in order[:8]]
    n = max(len(p) for p in show)
    frames = []
    for t in range(0, n, 4):
        img = base.copy()
        d = ImageDraw.Draw(img)
        for rank in range(len(show) - 1, -1, -1):
            p = show[rank]
            x, y, h = p[min(t, len(p) - 1)]
            d.polygon(car_shape(x, y, h), fill=(255, 179, 0) if rank == 0 else (180, 200, 255))
        d.rounded_rectangle([10, 10, 20 + 10 * len(title), 44], 8, fill=(0, 0, 0))
        d.text((20, 16), title, fill=(255, 255, 255), font=FONT)
        frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=32))
        if len(frames) >= 300:
            break
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=33, loop=0, optimize=True)


def save_replay(track, paths, fits, gen, path):
    order = np.argsort(-fits)[:10]
    data = {"w": W, "h": H, "half": HALF_WIDTH, "track": [[round(a, 1), round(b, 1)] for a, b in track.pts],
            "gen": gen, "cars": [[[round(x, 1), round(y, 1), round(h, 2)] for x, y, h in paths[i]] for i in order],
            "laps": [round(float(fits[i]), 3) for i in order]}
    html = """<!doctype html><html><head><meta charset="utf-8"><title>Harvest car race</title>
<style>body{margin:0;background:#111;color:#eee;font:14px system-ui;display:grid;place-items:center;min-height:100vh}
canvas{max-width:96vw;border-radius:12px}p{margin:8px}</style></head><body><div>
<canvas id="c"></canvas><p id="t"></p></div><script>
const D=__DATA__, c=document.getElementById("c"), x=c.getContext("2d");
c.width=D.w; c.height=D.h; let t=0;
function road(){x.fillStyle="#2e5e32";x.fillRect(0,0,D.w,D.h);x.lineJoin="round";
 for(const [w,col] of [[D.half*2+6,"#ebebeb"],[D.half*2,"#46484c"]]){x.lineWidth=w;x.strokeStyle=col;x.beginPath();
 D.track.forEach((p,i)=>i?x.lineTo(p[0],p[1]):x.moveTo(p[0],p[1]));x.closePath();x.stroke();}}
function car(p,col){x.save();x.translate(p[0],p[1]);x.rotate(p[2]);x.fillStyle=col;x.beginPath();
 x.moveTo(10,0);x.lineTo(-7,6);x.lineTo(-4,0);x.lineTo(-7,-6);x.closePath();x.fill();x.restore();}
const n=Math.max(...D.cars.map(c=>c.length));
function frame(){road();for(let i=D.cars.length-1;i>=0;i--){const p=D.cars[i];car(p[Math.min(t,p.length-1)],i?"#b4c8ff":"#ffb300");}
 document.getElementById("t").textContent="Generation "+D.gen+": the 10 best cars. Gold drove "+D.laps[0]+" laps in 40 seconds.";
 t=(t+1)%(n+40);requestAnimationFrame(frame);}
frame();</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html.replace("__DATA__", json.dumps(data, separators=(",", ":"))))


# --- evolution ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generations", type=int, default=int(os.environ.get("HARVEST_STEPS") or 120))
    ap.add_argument("--population", type=int, default=80)
    ap.add_argument("--mutation", type=float, default=0.15, help="size of the random changes")
    ap.add_argument("--hidden", type=int, default=8, help="neurons in each car's brain")
    ap.add_argument("--track", type=int, default=4, help="which track (any number)")
    ap.add_argument("--seed", type=int, default=0)
    args, _ = ap.parse_known_args()  # ignore settings this script does not use
    os.makedirs(OUT, exist_ok=True)

    track = Track(args.track)
    G, P, E = genome_size(args.hidden), args.population, max(2, args.population // 10)
    ckpt = os.path.join(OUT, "checkpoint.npz")
    start, best_ever = 0, -1.0
    if os.path.exists(ckpt):
        c = np.load(ckpt)
        if c["pop"].shape == (P, G):
            pop, start, best_ever = c["pop"], int(c["gen"]), float(c["best"])
            say({"log": f"continuing from generation {start} (best so far {best_ever:.2f} laps)"})
    if start == 0:
        pop = np.random.default_rng(args.seed).normal(0, 0.6, (P, G))
    say({"log": f"track {args.track}: {track.length:.0f} px a lap; {P} cars, {args.hidden} neurons, mutation {args.mutation}"})
    say({"progress": {"step": start, "value": max(best_ever, 0), "total": args.generations}})

    gif_every = max(5, args.generations // 5)
    for gen in range(start, args.generations):
        t0 = time.time()
        fit, _ = race(track, pop, args.hidden)
        order = np.argsort(-fit)
        best = float(fit[order[0]])
        if gen % gif_every == 0:
            top = pop[order[:8]]
            f2, paths = race(track, top, args.hidden, record=True)
            save_gif(track, paths, f2, f"Generation {gen + 1}: best {best:.2f} laps", os.path.join(OUT, f"gen_{gen + 1:04d}.gif"))
        if best > best_ever:
            best_ever = best
            with open(os.path.join(OUT, "best_car.json"), "w") as f:
                json.dump({"generation": gen + 1, "laps": best, "hidden": args.hidden, "track": args.track,
                           "genome": pop[order[0]].round(5).tolist()}, f)
        # Next generation: keep the best few, breed the rest from strong parents.
        rng = np.random.default_rng([args.seed, gen])
        nxt = [pop[order[:E]]]
        k = P - E
        a = rng.integers(0, P, (k, 3))
        b = rng.integers(0, P, (k, 3))
        pa = pop[a[np.arange(k), fit[a].argmax(1)]]
        pb = pop[b[np.arange(k), fit[b].argmax(1)]]
        child = np.where(rng.random((k, G)) < 0.5, pa, pb)
        child += rng.normal(0, args.mutation, (k, G)) * (rng.random((k, G)) < 0.3)
        nxt.append(child)
        pop = np.concatenate(nxt)
        np.savez(ckpt, pop=pop, gen=gen + 1, best=best_ever)
        say({"episode": {"index": gen, "reward": round(best, 4), "steps": P,
                         "extra": {"avg_laps": round(float(fit.mean()), 4),
                                   "full_laps": int((fit >= 1).sum()), "seconds": round(time.time() - t0, 2)}}})

    fit, _ = race(track, pop, args.hidden)
    top = pop[np.argsort(-fit)[:10]]
    f2, paths = race(track, top, args.hidden, record=True)
    save_gif(track, paths, f2, f"Final: best {f2.max():.2f} laps", os.path.join(OUT, "best.gif"))
    save_replay(track, paths, f2, args.generations, os.path.join(OUT, "replay.html"))
    say({"log": f"done: best {best_ever:.2f} laps. Download the outputs and open replay.html or best.gif."})


if __name__ == "__main__":
    main()
