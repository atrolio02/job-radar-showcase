"""Last-resort alert, run by each workflow's final step only when an earlier step failed.

It catches what the Python error tracker can't: a failed install, a crash before
tracking started, or a failed save. It uses only Python's standard library, so it
works even if `pip install` was the step that broke.

If the error tracker already sent an alert for this run, this does nothing.
"""
import json
import os
import urllib.request
from pathlib import Path

env = os.environ.get


def _call(url: str, body: dict | None = None, token: str | None = None, method: str = "POST"):
    headers = {"Content-Type": "application/json", "User-Agent": "job-radar"}
    if token:
        headers |= {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def main() -> None:
    if Path(env("RUNNER_TEMP", "/nonexistent"), "jobradar-alerted").exists():
        print("The error tracker already sent an alert for this run.")
        return
    workflow, repo = env("GITHUB_WORKFLOW", "A workflow"), env("GITHUB_REPOSITORY", "")
    run_url = f"{env('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{env('GITHUB_RUN_ID', '')}"
    fp = "wf-" + "".join(c for c in workflow.lower() if c.isalnum())[:12]
    title = f"Job Radar: '{workflow}' failed"
    body = (f"The **{workflow}** run failed in a step outside Job Radar's own code "
            f"(setting up, installing, or saving results), so it couldn't report the details itself.\n\n"
            f"Open the run and look for the step with a red ✗: {run_url}")

    hook = env("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            _call(hook, {"embeds": [{"title": f"🚨 {title}", "description": body, "color": 0xE74C3C}]})
        except Exception as e:  # noqa: BLE001
            print(f"Discord alert failed: {e}")

    token = env("GITHUB_TOKEN")
    if token and repo:
        api = f"https://api.github.com/repos/{repo}"
        try:
            open_issues = _call(f"{api}/issues?labels=radar-error&state=open&per_page=100", token=token, method="GET") or []
            match = next((i for i in open_issues if f"[{fp}]" in i.get("title", "")), None)
            if match:
                _call(f"{api}/issues/{match['number']}/comments", {"body": "Happened again.\n\n" + body}, token)
            else:
                _call(f"{api}/issues", {"title": f"⚠️ {title} [{fp}]", "body": body, "labels": ["radar-error"]}, token)
        except Exception as e:  # noqa: BLE001
            print(f"GitHub issue failed: {e}")


if __name__ == "__main__":
    main()
