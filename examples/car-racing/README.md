# Cars learning a race track

80 little cars learn to drive a twisty track by evolution. Each car has five
distance sensors and a tiny neural network that steers, accelerates and
brakes. The cars that get furthest in a 40-second race become the parents of
the next generation. Generation 1 crashes at the first corner; by generation
60 the best cars do more than 4 laps.

## Run it on Harvest

1. **New task** > Python > **Folder** > pick this `car-racing` folder.
2. File to run: `car_race.py`. Expected steps: `100` (generations).
3. Start. Each generation is one point on the chart (laps in 40 seconds).
4. When it finishes, download the outputs:
   - `replay.html`: open in a browser to watch the 10 best cars race.
   - `best.gif` and `gen_XXXX.gif`: snapshots of how driving improved.
   - `best_car.json`: the winning brain.

About 2 seconds per generation on a normal PC. If a game pauses it, it carries
on from its checkpoint, on the same PC or another one.

## Try a sweep

Open "Try different settings" and paste, for example:

```
mutation = 0.05, 0.15, 0.3
hidden = 4, 8, 16
```

That is 9 runs spread over your machines. The Sweeps page then shows which
settings learn fastest. Other settings: `population` (cars per generation),
`track` (any number gives a different track), `seed`.

## Run it locally

```
pip install numpy pillow
python car_race.py --generations 60
```
