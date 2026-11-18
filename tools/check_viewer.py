"""Optional real-browser check. Requires Chromium and Python's websocket-client."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import math
from pathlib import Path
import statistics
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import websocket


def http_json(url, method="GET"):
    with urlopen(Request(url, method=method), timeout=60) as reply:
        return json.load(reply)


class Page:
    def __init__(self, url):
        self.socket = websocket.create_connection(url, timeout=20, suppress_origin=True)
        self.serial = 0
        self.responses = {}

    def call(self, method, **params):
        self.serial += 1
        self.socket.send(json.dumps(dict(id=self.serial, method=method, params=params)))
        while True:
            msg = json.loads(self.socket.recv())
            if msg.get("method") == "Network.responseReceived":
                data = msg["params"]
                self.responses[data["response"]["url"]] = data["requestId"]
            if msg.get("id") == self.serial:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]

    def js(self, expression):
        reply = self.call("Runtime.evaluate", expression=expression,
                          returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in reply:
            raise RuntimeError(reply["exceptionDetails"])
        return reply["result"].get("value")

    def wait(self, expression):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = self.js(expression)
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError("Timed out: " + expression)

    def click_at(self, x, y):
        for action, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self.call("Input.dispatchMouseEvent", type=action, x=x, y=y, button="left",
                      buttons=buttons, clickCount=1)

    def click(self, selector):
        el = f"document.querySelector({json.dumps(selector)})"
        self.wait(f"!!{el} && !{el}.disabled")
        rect = self.js(f"""(() => {{
          const el = {el};
          el.scrollIntoView({{block:'center', behavior:'instant'}});
          const r = el.getBoundingClientRect();
          const x = r.x+r.width/2, y = r.y+r.height/2;
          if (!el.contains(document.elementFromPoint(x,y))) throw Error('Target obscured');
          return [x, y];
        }})()""")
        self.click_at(*rect)

    def navigate(self, url):
        self.call("Page.navigate", url=url)
        self.wait(f"location.href.split('?')[0] === {json.dumps(url.split('?')[0])}"
                  " && document.readyState === 'complete'")


@contextmanager
def browser_page(binary, scratch):
    profile = scratch / "profile"
    flags = ["--headless=new", "--disable-gpu", "--disable-extensions", "--no-first-run",
             "--no-default-browser-check", "--disable-background-networking",
             "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
             "--force-device-scale-factor=1", f"--user-data-dir={profile}", "about:blank"]
    with subprocess.Popen([str(binary), *flags], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
        page = None
        try:
            endpoint = profile / "DevToolsActivePort"
            for _ in range(300):
                if proc.poll() is not None:
                    raise RuntimeError(f"Headless browser exited ({proc.returncode})")
                if endpoint.exists() and len(endpoint.read_text().splitlines()) >= 2:
                    break
                time.sleep(0.05)
            port = int(endpoint.read_text().splitlines()[0])
            targets = http_json(f"http://127.0.0.1:{port}/json/list")
            target = next(t for t in targets if t["url"] == "about:blank" and t["type"] == "page")
            page = Page(target["webSocketDebuggerUrl"])
            page.call("Page.enable")
            page.call("Network.enable")
            page.call("Emulation.setDeviceMetricsOverride", width=1440, height=1080,
                      deviceScaleFactor=1, mobile=False)
            yield page
        finally:
            if page:
                try:
                    page.call("Browser.close")
                except websocket.WebSocketException:
                    pass
                finally:
                    page.socket.close()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()


def check_fields(page, scope, row):
    for key in ("action", "actual", "target", "verdict"):
        selector = f"{scope} [data-field={key}]"
        el = f"document.querySelector({json.dumps(selector)})"
        visible = page.js(f"{el}?.innerText.trim()")
        if visible is None or str(row[key]) != visible.replace(",", ""):
            raise AssertionError(f"{selector}: displayed {visible!r}, grader returned {row[key]!r}")


def check_memory_source(page, data):
    page.click("#timeline button:last-child")
    blind = data["blind"]
    hit = blind["retrieved"]
    work = page.js("document.querySelector('#memory .layer').innerText")
    assert blind["session"] in work and f"修订 #{hit['revision_id']}" in work
    revision = f"#memory details[data-revision-id='{hit['revision_id']}']"
    page.click(revision + " summary")
    rule = revision + f" [data-skill-id='{hit['skill_id']}']"
    source = page.js(f"document.querySelector({json.dumps(rule)}).innerText")
    for label, key in (("技能 #", "skill_id"), ("观察 #", "observation_id"), ("事件 ", "event_id")):
        assert label + str(hit[key]) in source, source
    page.click(rule + " button")
    scope = f"#execution-detail[data-event-key={json.dumps(hit['event_key'])}]"
    page.wait(f"!!document.querySelector({json.dumps(scope)})")
    trial = next(r for r in data["initial"] + data["attempts"] if r["event_key"] == hit["event_key"])
    check_fields(page, scope, trial)


def check_create_and_reopen(page, url):
    page.navigate(url + "/")
    page.wait("!document.querySelector('#create-run').disabled && !!document.querySelector('[data-summary=A]')")
    previous_id = page.js("document.querySelector('#run-picker').value")
    previous = http_json(f"{url}/api/runs/{previous_id}")
    page.click("#create-run")
    new_id = page.wait(f"""(() => {{
        const picker = document.querySelector('#run-picker');
        return !picker.disabled && picker.value !== {json.dumps(previous_id)} && picker.value;
    }})()""")
    fresh = http_json(f"{url}/api/runs/{new_id}")
    check_fields(page, "[data-summary=A]", fresh["environments"]["A"]["blind"])
    # The picker prepends the new run. A real ArrowDown selects the preceding run.
    page.js("document.querySelector('#run-picker').focus()")
    for action in ("keyDown", "keyUp"):
        page.call("Input.dispatchKeyEvent", type=action, key="ArrowDown", code="ArrowDown",
                  windowsVirtualKeyCode=40)
    page.wait("!document.querySelector('#run-picker').disabled && "
              f"new URL(location).searchParams.get('run') === {json.dumps(previous_id)}")
    for env in ("A", "B"):
        page.click(f"[data-environment={env}]")
        blind = previous["environments"][env]["blind"]
        assert blind["session"] != fresh["environments"][env]["blind"]["session"]
        assert blind["session"] in page.js("document.querySelector('#memory').innerText")
        check_fields(page, f"[data-summary={env}]", blind)


def check_replay(page, url, screenshot):
    page.navigate(url + "/")
    page.wait("document.querySelector('[data-summary=A] [data-field=verdict]')?.innerText")
    prefix = url + "/api/runs/"
    run_url = next(k for k in page.responses if k.startswith(prefix) and "/" not in k[len(prefix):])
    run = http_json(run_url)
    for env, data in run["environments"].items():
        check_fields(page, f"[data-summary={env}]", data["blind"])

    # Reopen the permalink in a fresh document and verify both the run and its grades.
    permalink = page.js("location.href")
    page.navigate("about:blank")
    page.navigate(permalink)
    page.wait(f"document.querySelector('#run-picker').value === {json.dumps(run['id'])}"
              " && document.querySelector('[data-summary=B] [data-field=verdict]')?.innerText")
    for env, data in run["environments"].items():
        check_fields(page, f"[data-summary={env}]", data["blind"])

    screenshot.parent.mkdir(parents=True, exist_ok=True)
    png = page.call("Page.captureScreenshot", format="png")
    screenshot.write_bytes(base64.b64decode(png["data"]))
    results = {}
    for env in ("B", "A"):
        page.click(f"[data-environment={env}]")
        page.wait(f"document.querySelector('[data-environment={env}]').getAttribute('aria-pressed') === 'true'")
        data = run["environments"][env]
        check_memory_source(page, data)
        event = next(i for ids in data["result"]["counterexamples"].values() for i in ids)
        attempt = next(a for a in data["attempts"] if a["event_id"] == event)
        key = json.dumps(attempt["event_key"])
        page.click(f"[data-summary={env}] [data-counterexample={key}]")
        scope = f"#execution-detail[data-event-key={key}]"
        page.wait(f"!!document.querySelector({json.dumps(scope)})")
        page.wait("document.activeElement.id === 'step-detail' && "
                  "document.querySelector('#execution-detail').getBoundingClientRect().top < innerHeight")
        check_fields(page, scope, attempt)
        compare_url = f"{run_url}/{env}/compare"
        sessions = set()
        for _ in range(2):
            page.click("#compare-button")
            page.wait("!document.querySelector('#compare-button').disabled && "
                      "document.querySelector('#sha-check')?.innerText && "
                      "document.querySelector('[data-branch=disabled] [data-field=verdict]')?.innerText")
            request_id = page.responses[compare_url]
            body = page.call("Network.getResponseBody", requestId=request_id)["body"]
            comparison = json.loads(body)
            assert comparison["sha_before"] == comparison["sha_after"] == data["frozen_sha"]
            for name in ("enabled", "disabled"):
                check_fields(page, f"[data-branch={name}]", comparison[name])
                session = comparison[name]["session"]
                assert session not in sessions, "Expected a new read-only Agent session"
                sessions.add(session)
        results[env] = {name: {k: comparison[name][k] for k in
                               ("action", "policy", "actual", "target", "verdict")}
                        for name in ("enabled", "disabled")}
    return run_url + "/A/compare", results


# This file page remains unchanged throughout both conditions. The second rAF
# follows a rendering opportunity for the updated, visible click counter.
LIGHT_PAGE = """<!doctype html><meta charset=utf-8><button style="width:160px;height:80px">0</button>
<script>
const button = document.querySelector('button');
button.onclick = (e) => {
    if (!e.isTrusted) throw Error('Untrusted click');
    button.textContent = Number(button.textContent) + 1;
    window.rendered = new Promise(resolve => requestAnimationFrame(() =>
        requestAnimationFrame(() => resolve(Number(button.textContent)))));
};
</script>"""


def sample(page):
    start = time.perf_counter()
    page.click_at(60, 40)
    value = page.js("window.rendered")
    return value, (time.perf_counter() - start) * 1000


def benchmark(page, scratch, compare_url):
    test_page = scratch / "latency.html"
    test_page.write_text(LIGHT_PAGE, encoding="utf-8")
    page.navigate(test_page.as_uri())
    count = 0

    def measure():
        nonlocal count
        observed, elapsed = sample(page)
        count += 1
        if observed != count:
            raise AssertionError("Click counter failed to update")
        return elapsed

    for _ in range(8):
        measure()
    idle, busy = [], []
    requests = 0
    with ThreadPoolExecutor(max_workers=1) as pool:
        for block in range(1, 5):
            goal = block * 8
            while len(idle) < goal:
                idle.append(measure())
            while len(busy) < goal:
                if requests >= 64:
                    raise RuntimeError("Could not capture enough complete in-flight samples")
                task = pool.submit(http_json, compare_url, "POST")
                requests += 1
                time.sleep(0.025)
                while not task.done() and len(busy) < goal:
                    elapsed = measure()
                    if not task.done():
                        busy.append(elapsed)
                task.result()

    def summarize(values):
        return dict(n=len(values), median_ms=round(statistics.median(values), 2),
                    p95_ms=round(sorted(values)[math.ceil(.95*len(values))-1], 2),
                    max_ms=round(max(values), 2))

    return dict(idle=summarize(idle), replay=summarize(busy), comparison_requests=requests)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Running MemoryFork loopback URL")
    parser.add_argument("--browser", required=True, type=Path, help="Chrome or Edge executable")
    parser.add_argument("--screenshot", required=True, type=Path)
    parser.add_argument("--check-create", action="store_true",
                        help="Also create a real run and switch back; use a disposable viewer")
    args = parser.parse_args()
    if urlparse(args.url).hostname != "127.0.0.1":
        parser.error("Pass the local viewer at 127.0.0.1")
    with tempfile.TemporaryDirectory(prefix="memoryfork-browser-") as tmp:
        with browser_page(args.browser, Path(tmp)) as page:
            browser = page.call("Browser.getVersion")["product"]
            endpoint, results = check_replay(page, args.url.rstrip("/"), args.screenshot)
            if args.check_create:
                check_create_and_reopen(page, args.url.rstrip("/"))
            timings = benchmark(page, Path(tmp), endpoint)
            print(json.dumps(dict(browser=browser, environments=results, latency=timings), indent=2))


if __name__ == "__main__":
    main()
