# Harvest — worker agent

[Harvest](https://harvest.codevexx.com) is a service that puts the PCs you
already own to work. You sign in, connect your machines, and start tasks (such
as reinforcement-learning training) from the dashboard. Each machine runs tasks
while it is idle and **pauses the moment you start a game**.

This repository is the **worker agent** — the small program you install on each
PC to connect it to your Harvest account. The service itself is hosted at
harvest.codevexx.com; you don't run any server yourself.

## Install

On each PC, install [Python 3.11+](https://www.python.org/downloads/), then:

```bash
git clone https://github.com/codevexx/harvest
cd harvest
python -m venv .venv
# Windows:  .\.venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate
pip install requests psutil gymnasium numpy
```

Only install PyTorch if your tasks need it. On an NVIDIA machine use the CUDA
build (`pip install torch --index-url https://download.pytorch.org/whl/cu124`);
on an AMD GPU on Windows use the CPU build (`pip install torch`).

## Connect

1. Sign in at <https://harvest.codevexx.com/app> and create a farm.
2. Click **Connect a machine** and copy the connect token (shown once).
3. Run the agent on the PC:

```bash
python -m worker.agent --coordinator https://harvest.codevexx.com --token YOUR_TOKEN --name my-pc
```

The machine shows up on your dashboard and starts taking tasks when it's idle.
Run the same command on each PC (settings can also go in
`worker/worker.config.json`).

## Idle / gaming behaviour

The agent picks a mode every few seconds:

| Mode | When | Behaviour |
|------|------|-----------|
| full | no keyboard/mouse for a while | runs tasks at full power |
| light | you're using the PC | runs CPU-only at low priority |
| paused | a game or fullscreen app is running | stops tasks, re-queues them to finish later |

List your games in `worker/worker.config.json` so they trigger a pause by name:

```json
{ "idle_threshold": 300, "pause_on_fullscreen": true, "games": ["cs2.exe", "RocketLeague.exe"] }
```

## Writing your own tasks

Start tasks from the dashboard: pick a built-in template (like CartPole) or
paste your own Python. To adapt existing code, print one JSON line per episode:

```python
print(json.dumps({"episode": {"index": i, "reward": r, "steps": s}}), flush=True)
```

Full guide: <https://harvest.codevexx.com/compat>. A complete example lives in
[`examples/cartpole/agent.py`](examples/cartpole/agent.py).

## Ground rule

Point tasks at environments you own or control — your own game, a local
simulation, or a standard benchmark like CartPole. Harvest is for training and
compute on hardware and targets you're allowed to use.
