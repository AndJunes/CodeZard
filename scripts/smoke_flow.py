"""The whole flow, from an idea to a ZIP, against a running stack.

    python scripts/smoke_flow.py                    # http://127.0.0.1:8090
    python scripts/smoke_flow.py --gateway URL      # somewhere else
    python scripts/smoke_flow.py --idea "..."       # a different scenario

It lives here and not in a scratch directory because the gateway owns the flow, and it drives
only `/runs` — no agent tokens, exactly as the browser does. What it proves is that every part
holds together at once, which no unit test can.

THE SCENARIO IS FIXED ON PURPOSE

One entity, four scalar fields, a REST resource. That is the shape the backend agent's CRUD
probe exercises: seven real HTTP operations against the server the model just wrote. A vaguer
idea produces a vaguer plan, a bigger file list, and a run whose failures are about the
scenario rather than about the code.

WHAT COUNTS AS PASSING

Not "it answered". A ZIP that arrives with half its files, or whose tests are red, is the
thing this project spent a day learning to stop calling a delivery. So: every planned file
present, a README, tests, no file that lost its line breaks, and a verdict that is not one of
the rejecting ones.

With the agents offline (`MIRAG_OFFLINE=1`) the whole thing runs in seconds with no key and no
network, and the verdict says `simulated`. That is a check of the PLUMBING. The real close
needs the agents online.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from typing import Any

IDEA = ("Una API REST para gestionar reservas de salas de reunión, con CRUD completo. "
        "Cada reserva tiene: sala, persona, fecha y hora de inicio. Solo biblioteca "
        "estándar de Python, datos en SQLite, arquitectura por dominio.")

ANSWERS = [
    "Una recepcionista, nadie más.",
    "Unas 50 reservas por semana, una sola sede.",
    "Se anotan en papel y se pisan.",
    "Ver las reservas del día y que no se dupliquen.",
]

REJECTED = {"FAILED", "INCOMPLETE"}
"""Verdicts that mean there is nothing to deliver. They must not come with a download."""


class Flow:
    def __init__(self, gateway: str) -> None:
        self.gateway = gateway.rstrip("/")
        self.failures: list[str] = []

    # ── saying what happened ─────────────────────────────────────────────────

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"    {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)
        return ok

    # ── talking to the gateway ───────────────────────────────────────────────

    def call(self, path: str, method: str = "POST", body: dict | None = None,
             timeout: int = 1800) -> tuple[int, Any]:
        request = urllib.request.Request(
            f"{self.gateway}/runs{path}", method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
                status = response.status
        except urllib.error.HTTPError as error:
            payload, status = json.loads(error.read() or b"{}"), error.code
        print(f"  {method} /runs{path or ''} → {status} in {time.monotonic() - started:.0f}s")
        return status, payload

    def stream(self, path: str, timeout: int = 1800) -> list[dict[str, Any]]:
        request = urllib.request.Request(f"{self.gateway}/runs{path}", method="POST",
                                         headers={"Content-Type": "application/json"})
        events, started = [], time.monotonic()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for line in response:
                if line.startswith(b"data: "):
                    events.append(json.loads(line[6:]))
        print(f"  POST /runs{path} → {len(events)} events in {time.monotonic() - started:.0f}s")
        return events

    def download(self, artifact: str, token: str) -> bytes:
        request = urllib.request.Request(
            f"{self.gateway}/api/backend/api/v1/artifacts/{artifact}/download",
            headers={"X-Mirag-Token": token} if token else {})
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()

    # ── the flow ─────────────────────────────────────────────────────────────

    def run(self, idea: str, token: str) -> int:
        print("\n=== 1. an idea becomes a run ===")
        status, run = self.call("", body={"idea": idea})
        if not self.check("the run started", status == 200, json.dumps(run)[:120]):
            return 1
        run_id = run["runId"]
        print(f"    {run['summary'][:100]}")

        print("\n=== 2. the gate, before there is a plan ===")
        status, _ = self.call(f"/{run_id}/generation")
        self.check("generation without a plan is refused", status == 409, f"got {status}")

        print("\n=== 3. the answers become a plan ===")
        rounds = 0
        while run.get("state") == "QUESTIONNAIRE" and rounds < 3:
            asked = (run.get("questionnaire") or {}).get("questions") or []
            given = [{"questionId": q["id"], "value": ANSWERS[i] if i < len(ANSWERS)
                      else "No tengo preferencia."} for i, q in enumerate(asked)]
            status, run = self.call(f"/{run_id}/answers", body={"answers": given})
            if status != 200:
                self.check("the plan came back", False, json.dumps(run)[:140])
                return 1
            rounds += 1
        plan = run.get("plan") or {}
        self.check("a real plan came back", bool(plan.get("purpose") and plan.get("entities")))
        print(f"    entidades: {', '.join(e['name'] for e in plan.get('entities') or [])}")

        print("\n=== 4. the gate, with a plan that is not approved ===")
        status, _ = self.call(f"/{run_id}/generation")
        self.check("generation before approval is refused", status == 409, f"got {status}")

        print("\n=== 5. approval ===")
        status, run = self.call(f"/{run_id}/approval", body={})
        self.check("approval moves the run", run.get("state") == "PLAN_APPROVED",
                   str(run.get("state")))

        print("\n=== 6. generation ===")
        events = self.stream(f"/{run_id}/generation")
        for event in events:
            if event.get("type") == "step" and _interesting(event["name"]):
                print(f"      {event['name']:<24} {event['status']:<9} "
                      f"{str(event.get('summary'))[:90]}")

        print("\n=== 7. what the server kept ===")
        _, run = self.call(f"/{run_id}", method="GET")
        verdict = _verdict(events)
        self.check("the run reached ZIP_READY", run.get("state") == "ZIP_READY",
                   f"{run.get('state')} {str(run.get('error'))[:60]}")
        print(f"    verdict: {verdict or '(none)'}")

        print("\n=== 8. a reconnecting tab can replay it ===")
        replayed = self._replay(run_id)
        self.check("the stream was kept", replayed > 0, f"{replayed} events")

        if not run.get("artifactId"):
            self.check("there is an artifact", False, "none")
            return 1

        print("\n=== 9. the ZIP ===")
        return self._inspect(self.download(run["artifactId"], token), events, verdict)

    def _replay(self, run_id: str) -> int:
        request = urllib.request.Request(f"{self.gateway}/runs/{run_id}/events")
        with urllib.request.urlopen(request, timeout=60) as response:
            return sum(1 for line in response if line.startswith(b"data: "))

    def _inspect(self, data: bytes, events: list[dict[str, Any]], verdict: str) -> int:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            contents = {n: archive.read(n) for n in names}
        lines = sum(body.count(b"\n") for body in contents.values())
        print(f"    {len(data):,} bytes · {len(names)} files · {lines} lines")

        self.check("there is a README",
                   any(n.upper().endswith("README.MD") for n in names))
        self.check("there are tests",
                   any("/tests/" in n or n.startswith("tests/") for n in names))
        self.check("no file lost its line breaks",
                   not [n for n, body in contents.items()
                        if n.endswith(".py") and len(body) > 200 and b"\n" not in body])
        self.check("the verdict does not reject it", verdict not in REJECTED, verdict)

        planned = _planned(events)
        missing = sorted(planned - {n.split("/", 1)[-1] for n in names})
        self.check("every planned file is in the ZIP", not missing, str(missing[:4]))
        return 1 if self.failures else 0


def _interesting(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in (
        "specification", "generation", "project", "plan", "structure", "syntax", "imports",
        "tests", "crud", "repair", "packaging", "artifact"))


def _verdict(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        project = event.get("project")
        if event.get("type") == "done" and isinstance(project, dict):
            return str(project.get("status") or "")
    return ""


def _planned(events: list[dict[str, Any]]) -> set[str]:
    """The blueprint's own file list, from the specification step."""
    for event in events:
        detail = event.get("detail")
        if event.get("name") == "specification" and isinstance(detail, dict):
            return {str(p) for p in (detail.get("plan") or [])}
    return set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="http://127.0.0.1:8090")
    parser.add_argument("--idea", default=IDEA)
    parser.add_argument("--token", default="", help="only the download needs one")
    args = parser.parse_args()

    flow = Flow(args.gateway)
    try:
        code = flow.run(args.idea, args.token)
    except urllib.error.URLError as error:
        print(f"\nthe gateway at {args.gateway} did not answer: {error}", file=sys.stderr)
        return 2
    print("\n" + ("ALL PASS" if not flow.failures
                  else f"{len(flow.failures)} FAILED: {flow.failures}"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
