"""
Local dashboard for the NZ Legislation API.

    python dashboard.py            # live, needs NZL_API_KEY in .env
    python dashboard.py --demo     # canned sample data, no key needed

Serves on http://127.0.0.1:8050. The API key stays server-side: the browser
only talks to this process, which proxies to api.legislation.govt.nz.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from legislation import NZLegislation, RateLimited, Version, Work

HERE = Path(__file__).parent
PAGE = HERE / "dashboard.html"

STATUS_PARAM = {
    "act": "act_status",
    "bill": "bill_status",
    "secondary_legislation": "legislation_status",
}


def work_to_dict(w: Work) -> dict[str, Any]:
    v = w.latest_matching
    return {
        "work_id": w.work_id,
        "title": w.title,
        "type": w.legislation_type,
        "status": w.status,
        "agencies": w.agencies,
        "publisher": w.publisher,
        "version_date": v.version_date if v else None,
        "html_url": w.html_url,
        "pdf_url": v.formats.get("pdf") if v else None,
    }


def version_to_dict(v: Version) -> dict[str, Any]:
    return {"version_id": v.version_id, "date": v.version_date, "title": v.title, "formats": v.formats}


class Backend:
    """Live API access. A lock keeps the client's rate spacing honest across threads."""

    def __init__(self, client: NZLegislation):
        self.client = client
        self.lock = threading.Lock()

    def search(self, q: dict[str, str]) -> dict[str, Any]:
        kind = q.get("type") or None
        kwargs: dict[str, Any] = {
            "search_term": q.get("term") or None,
            "legislation_type": kind,
            "sort_by": q.get("sort") or None,
            "page": int(q.get("page", "1")),
            "per_page": 20,
        }
        status = q.get("status")
        if status and kind in STATUS_PARAM:
            kwargs[STATUS_PARAM[kind]] = status
        with self.lock:
            works, total = self.client.search(**kwargs)
        return {
            "total": total,
            "page": kwargs["page"],
            "per_page": 20,
            "results": [work_to_dict(w) for w in works],
            "remaining": self.client.remaining,
        }

    def versions(self, work_id: str) -> dict[str, Any]:
        with self.lock:
            vers = self.client.versions(work_id)
        return {"results": [version_to_dict(v) for v in vers], "remaining": self.client.remaining}


class DemoBackend:
    """Sample data for working on the UI without spending quota. Not real API output."""

    WORKS = [
        ("act_public_2015_70", "Health and Safety at Work Act 2015", "act", "in_force",
         ["Ministry of Business, Innovation, and Employment"], "2024-04-05"),
        ("act_public_2020_31", "Privacy Act 2020", "act", "in_force", ["Ministry of Justice"], "2024-11-28"),
        ("act_public_2000_24", "Employment Relations Act 2000", "act", "in_force",
         ["Ministry of Business, Innovation, and Employment"], "2025-02-14"),
        ("act_public_1993_28", "Privacy Act 1993", "act", "repealed", ["Ministry of Justice"], "2020-12-01"),
        ("bill_government_2025_101", "Employment Relations Amendment Bill", "bill", "current",
         ["Ministry of Business, Innovation, and Employment"], "2025-06-24"),
        ("bill_government_2025_88", "Privacy Amendment Bill", "bill", "current", ["Ministry of Justice"], "2025-05-12"),
        ("sl_2016_13", "Health and Safety at Work (General Risk and Workplace Management) Regulations 2016",
         "secondary_legislation", "in_force", ["Ministry of Business, Innovation, and Employment"], "2023-11-02"),
    ]

    def _rows(self) -> list[dict[str, Any]]:
        return [
            {"work_id": wid, "title": t, "type": k, "status": s, "agencies": a, "publisher": "Parliamentary Counsel Office",
             "version_date": d, "html_url": "https://www.legislation.govt.nz/", "pdf_url": None}
            for wid, t, k, s, a, d in self.WORKS
        ]

    def search(self, q: dict[str, str]) -> dict[str, Any]:
        rows = self._rows()
        term = (q.get("term") or "").lower()
        if term:
            rows = [r for r in rows if term in r["title"].lower()]
        if q.get("type"):
            rows = [r for r in rows if r["type"] == q["type"]]
        if q.get("status"):
            rows = [r for r in rows if r["status"] == q["status"]]
        sort = q.get("sort")
        if sort == "title_asc":
            rows.sort(key=lambda r: r["title"])
        elif sort == "title_desc":
            rows.sort(key=lambda r: r["title"], reverse=True)
        else:
            rows.sort(key=lambda r: r["version_date"] or "", reverse=sort != "year_asc")
        return {"total": len(rows), "page": 1, "per_page": 20, "results": rows, "remaining": 9_876}

    def versions(self, work_id: str) -> dict[str, Any]:
        title = next((t for wid, t, *_ in self.WORKS if wid == work_id), work_id)
        dates = ["2025-02-14", "2024-04-05", "2023-08-30", "2022-12-16", "2021-07-01"]
        return {
            "results": [
                {"version_id": f"{work_id}_en_{d}", "date": d, "title": title,
                 "formats": {"html": "https://www.legislation.govt.nz/", "pdf": "https://www.legislation.govt.nz/"}}
                for d in dates
            ],
            "remaining": 9_875,
        }


def make_handler(backend: Backend | DemoBackend, demo: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict[str, Any]) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            try:
                if url.path == "/":
                    html = PAGE.read_text(encoding="utf-8").replace("__DEMO__", "true" if demo else "false")
                    self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")
                elif url.path == "/api/search":
                    self._json(HTTPStatus.OK, backend.search(q))
                elif url.path.startswith("/api/versions/"):
                    work_id = urllib.parse.unquote(url.path.removeprefix("/api/versions/"))
                    self._json(HTTPStatus.OK, backend.versions(work_id))
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except RateLimited as e:
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": f"Rate limited by the API ({e})"})
            except urllib.error.HTTPError as e:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"API returned {e.code} {e.reason}"})
            except (urllib.error.URLError, TimeoutError) as e:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Could not reach the API: {e}"})
            except ValueError as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="serve sample data instead of calling the API")
    ap.add_argument("--port", type=int, default=8050)
    args = ap.parse_args()

    backend: Backend | DemoBackend
    if args.demo:
        backend = DemoBackend()
    else:
        key = os.environ.get("NZL_API_KEY")
        if not key:
            sys.exit("NZL_API_KEY is not set. Add it to .env, or run with --demo.")
        backend = Backend(NZLegislation(key))

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(backend, args.demo))
    print(f"NZ Legislation dashboard on http://127.0.0.1:{args.port}{'  (demo data)' if args.demo else ''}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
