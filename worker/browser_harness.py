"""Website-bot tasks: open a URL in headless Chromium and run the user's script.

The harness source is kept in a string so the packaged app (PyInstaller, which
ships no .py files) can still write it next to the task and run it with the
machine's own Python, which has Playwright installed.

Contract for the user's script (the body of an async function, run in the page):
    harvest.log(...)     -> a line in the task output
    harvest.sleep(ms)    -> wait
    return 12            -> this run's score
    return {reward: 12, anything: 3}   -> score plus extra numbers
Each run reloads the page. RESUME_FROM skips runs already reported.
"""

HARNESS = r'''
import asyncio, json, os, sys

def out(obj):
    print(json.dumps(obj), flush=True)

try:
    from playwright.async_api import async_playwright
except ImportError:
    out({"log": "Playwright is not installed on this machine. In the Harvest app, open "
                "Settings and turn on website tasks (or run: pip install playwright && "
                "python -m playwright install chromium)."})
    sys.exit(2)

HELPERS = """
window.harvest = {
  log: (...a) => window.__harvestLog(a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ')),
  sleep: (ms) => new Promise(r => setTimeout(r, ms)),
};
"""

def score(result):
    if isinstance(result, bool):
        return float(result), {}
    if isinstance(result, (int, float)):
        return float(result), {}
    if isinstance(result, dict):
        extra = {k: v for k, v in result.items() if k != "reward" and isinstance(v, (int, float))}
        try:
            return float(result.get("reward", 0) or 0), extra
        except (TypeError, ValueError):
            return 0.0, extra
    return 0.0, {}

async def main():
    cfg = json.load(open("_harvest_job.json", encoding="utf-8"))
    script = open(cfg["script_file"], encoding="utf-8").read()
    runs, timeout = int(cfg.get("runs", 1)), float(cfg.get("timeout_s", 60))
    start = int(os.environ.get("RESUME_FROM", "0"))
    body = "async () => {\n" + script + "\n}"
    failures = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.get("headless", True))
        context = await browser.new_context(bypass_csp=True)
        await context.expose_function("__harvestLog", lambda s: out({"log": str(s)}))
        await context.add_init_script(HELPERS)
        page = await context.new_page()
        page.on("pageerror", lambda e: out({"log": "[page error] " + str(e)}))
        for i in range(start, runs):
            try:
                await page.goto(cfg["url"], wait_until="load", timeout=60000)
                result = await asyncio.wait_for(page.evaluate(body), timeout)
                reward, extra = score(result)
                failures = 0
            except asyncio.TimeoutError:
                out({"log": f"run {i}: hit the {timeout:.0f}s time limit"})
                reward, extra = 0.0, {"timeout": 1}
            except Exception as e:
                failures += 1
                out({"log": f"run {i}: {e}".splitlines()[0][:500]})
                if failures >= 3:
                    out({"log": "stopping: the script failed 3 runs in a row"})
                    await browser.close()
                    sys.exit(1)
                reward, extra = 0.0, {"error": 1}
            out({"episode": {"index": i, "reward": reward, "steps": 0, "extra": extra}})
        await browser.close()

asyncio.run(main())
'''
