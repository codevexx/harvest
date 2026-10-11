# Changelog

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
