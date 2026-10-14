# Changelog

## v0.13.0

- **Temperatures, power and energy cost.** NVIDIA machines now report GPU temperature and power draw (Linux also
  reports CPU temperature), shown on each machine on the dashboard with an estimate of the whole PC's power use.
  Each task adds up its estimated energy; set your electricity price (farm menu) to see what a task cost to run.
- **Linux tray icon.** On Linux desktops, install.sh adds a tray icon with Harvest's status, power mode (auto, full,
  light), pause, the dashboard and "show at login". It is a remote control for the background service, so closing
  it never stops the work. Skip it with `--no-tray`.

## v0.12.0

- **Clean up processes.** From a machine's menu on the dashboard, see what is using RAM and CPU and close what you
  don't need. Known background helpers (updaters, crash reporters, widgets) are suggested; everything else is your
  call, and open apps are asked to close normally so they can save first. Windows system processes, security
  software, drivers, Harvest itself and running tasks are never touched. On Windows you can also stop an app from
  starting with the computer (the same switch as Task Manager's Startup tab) and turn it back on later.

## v0.11.0

- **Secrets.** Save API keys your tasks need (like `ROBOFLOW_API_KEY` or `HF_TOKEN`) once per farm in the
  dashboard. They are stored encrypted, only sent to a machine running a task that asks for them, and show as
  `[secret]` in task output.
- **Starter templates.** Ready-made tasks you start by filling in a few fields: train YOLO on a Roboflow dataset,
  PPO reinforcement learning, a quick machine check and a page load timer. Your AI can start them too, through
  the Harvest MCP server.
- Machines report their NVIDIA GPU generation, so tasks that need a newer GPU (current PyTorch needs an RTX 20
  series or newer) only go to machines that can run them.

## v0.10.0

- **Live machine stats.** Harvest now reports each machine's CPU, memory and GPU load, so the new dashboard can
  show live gauges and history for every machine. Update Harvest on each machine to see its stats.

## v0.9.3

- **New look.** The Harvest app's panel matches the redesigned website and dashboard: dark, sharp and
  number-first, with the new leaf.

## v0.9.2

- Harvest-mac.dmg with the new "Install Harvest" window (the 0.9.1 Mac build did not finish). Everything else
  is as in 0.9.1.

## v0.9.1

- **New icon.** A redrawn Harvest leaf for the Windows and Mac apps, the Windows tray, the Mac menu bar and both
  installers.
- **Mac installer window.** Harvest-mac.dmg now opens a proper "Install Harvest" window: drag the app onto the
  Applications folder.
- **Branded Windows setup.** Harvest-Setup.exe shows the Harvest artwork instead of the generic installer images.

## v0.9.0

- **Harvest for Mac.** A menu bar app for Macs with Apple silicon: status, power mode, pause, the dashboard,
  start at login and automatic updates, one click away. Install with
  `curl -fsSL https://harvest.codevexx.com/install.sh | sh` (or download Harvest-mac.dmg). It replaces the
  command-line Mac service if you had it.
- **Mac GPUs.** Macs report their GPU (Metal), and tasks can ask for "NVIDIA (CUDA)", "Mac (Metal)" or "Any
  GPU". Every task gets `HARVEST_DEVICE` (`cuda`, `mps` or `cpu`), so the same PyTorch code uses whichever GPU
  the machine has.
- The dashboard shows macOS versions by name.

## v0.8.0

- **macOS and Linux.** `curl -fsSL https://harvest.codevexx.com/install.sh | sh` installs the agent for your user
  (no root) and runs it in the background: a LaunchAgent on macOS, a systemd user service on Linux (also at boot
  on servers). It updates itself from GitHub Releases, checking each download's SHA-256. Idle detection works on
  both (macOS input idle time; on Linux, GNOME, X11 or the login manager's idle hint, and a machine nobody is
  logged in to counts as idle).
- **Copies share their best results.** Tasks started with "Copies" or "Run on: Every machine" get
  `HARVEST_COPY`, `HARVEST_COPIES`, `HARVEST_SHARE_URL` and `HARVEST_SHARE_TOKEN`, so copies on different PCs can
  publish their best result and build on the best one found anywhere.

## v0.7.2

- The background copy of Harvest now also repairs locked runtime folders while the Harvest app is open, so PCs
  where the app was already running get fixed too.

## v0.7.1

- Fixed: the Harvest app could stay on "Connecting" after the background copy had installed Python (the folder
  was readable only by the background copy). Existing installs are repaired automatically.
- Fixed: an error window after Harvest restarted itself for an update.
- One unreadable runtime can no longer stop Harvest from connecting; the app shows the error instead.

## v0.7.0

- **Choose each PC's power mode.** Auto (as before), Full power, Light or Paused, from the Harvest app on the PC
  or from the machine's card on the dashboard. They stay in sync. Full power uses the whole PC even when someone
  is signed in (useful for a server that is always signed in but idle); games still pause tasks. Paused from the
  dashboard stops a running task within seconds and puts it back in the queue.

## v0.6.0

- **Continue on any machine.** When a task is paused (say, a game starts), Harvest saves what it has written so
  far (the `outputs/` folder and other files it created) as a checkpoint. If a different PC picks the task up, it
  restores that checkpoint first and continues from the same step. The checkpoint is deleted when the task ends.
- **Parameter sweeps.** Start one task with several values per setting (for example `lr = 0.001, 0.01, 0.1`) and
  Harvest runs every combination across your machines. Values arrive as `--name value` arguments (or wherever
  you put `{name}`), and as JSON in `HARVEST_PARAMS`. Website bots read them from `harvest.params`.

## v0.5.2

- **Results are kept even without `outputs/`.** If a task saves nothing into `outputs/`, the files it created
  in its folder (a Q-table, a model, a CSV) are uploaded instead, so you can download them from the dashboard.
- The chart's "Best" value is now exact on long runs (new best episodes are always sent).

## v0.5.1

- Tasks get their "Expected steps" value in `HARVEST_STEPS`, so a script can loop the number of times you set in the dashboard.
- Terminal colour codes are removed from task output.

## v0.5.0

- **Sets itself up.** If a PC has no Python or Node.js, Harvest downloads its own private copies (official
  Python from nuget.org, Node.js LTS from nodejs.org) plus Playwright and Chromium for website bots. Every
  download is checked against its published checksum and its publisher's digital signature, nothing needs
  admin rights, and it all lives in `%LOCALAPPDATA%\Harvest\runtimes`. Your own Python and Node are used
  first and never modified. Turn it off in Settings if you prefer.
- Website-bot support always goes into Harvest's own Python.
- The dashboard shows when a machine is still setting up.

## v0.4.0

- **Finds Python reliably.** Looks in PATH, the `py` launcher, the Windows registry and the usual install
  folders, and never uses the Microsoft Store placeholder. A PC without Python no longer accepts Python
  tasks; the dashboard tells you to install it.
- **Smarter task placement.** Tasks go to the best idle machine: nobody at the keyboard first, the machine
  idle longest next (so one PC no longer takes every task), GPU machines kept free while GPU work is
  waiting. Tasks can require minimum RAM or CPU threads, or prefer the most powerful machine.
- **Run on every machine,** or pin a task to one machine.
- **Any kind of task,** not just reinforcement learning: report `{"progress": {"step", "value", "total"}}`
  for a chart and progress bar. The RL `{"episode": ...}` format still works.
- **API for AI assistants and scripts:** create an API key in the dashboard and let an assistant start
  tasks for you. Guide: https://harvest.codevexx.com/api.txt

## v0.3.1

- Safer file handling: a PC refuses to unpack an upload that would expand past 250 MB, and output
  uploads report the server's reason (for example "Your storage is full") in the task output.
- Storage limits: each account has 100 MB for uploads and outputs, kept for 30 days. Outputs are checked
  on the server before they are accepted (no paths outside the folder, no links, no zip bombs) and
  download from a separate address as plain files.

## v0.3.0

- **Much less network traffic.** Idle PCs check in every 15 to 30 seconds with one request (was three
  every 3 seconds), and running tasks send progress in one batch every 10 seconds instead of one request per
  episode. Busy machines no longer show as offline during long tasks.
- **Python packages install themselves.** Include a `requirements.txt` and each PC builds a saved environment
  for it once, then reuses it. Packages you installed globally (like a CUDA build of torch) are reused too.
- **Download task outputs.** Files a task writes to `outputs/` (path in `HARVEST_OUTPUTS`) are zipped and
  uploaded when it finishes, up to 2 GB. Website bots can save files with `harvest.save(name, data)`.
- **Bigger uploads.** Up to 50 MB per task.
- Rename or remove machines from the dashboard.
- Pausing for a game during package installs stops cleanly and resumes later.

## v0.2.2

- Fix: turning on "Run before anyone signs in" could fail with error 0x80070534 when Windows reported the
  account's domain as WORKGROUP (for example over SSH).

## v0.2.1

**Works before anyone signs in.** Turn on "Run before anyone signs in" in the Harvest panel's settings (or
tick it in the installer) and Harvest starts with the PC through Task Scheduler. When you sign in, the tray
app takes over; when you sign out, the background copy picks the work back up.

- `Harvest.exe --connect <token> --name <pc>` connects a PC without opening any window.
- Each PC now keeps a single entry on the dashboard across restarts.
- A task that was running when a PC crashed or lost power goes back in the queue and resumes.
- Tasks use the real Python install, never the Microsoft Store placeholder.
- The app always writes a log to `%LOCALAPPDATA%\Harvest`.

## v0.2.0

**New Windows app.** Harvest now installs with `Harvest-Setup.exe` (no admin rights needed) and lives by the
clock: click the leaf for a panel with live status, Pause/Resume, and settings.

- Updates itself from GitHub Releases, verifying each download's SHA-256 first.
- Start with Windows, games to always pause for, and one-click website-task setup.
- New "held" mode when you press Pause, shown on the dashboard as "Paused by owner".

**New task types.**

- Python and Node.js: upload files or a whole folder, choose the file to run and its arguments.
- Website bots: open a URL in headless Chromium and run your JavaScript inside the page.
- Machines report what they can run, and tasks only go to machines that can run them.
- Stop a running task from the dashboard.

**Dashboard.** Redesigned with Google sign-in, an overview page, machine cards and a task side panel with
live charts and output. Built-in templates were removed.

## v0.1.0

First release: the tray agent for Windows, idle and game detection, and Python tasks.
