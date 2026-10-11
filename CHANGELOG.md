# Changelog

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
