"""
Client for the New Zealand Legislation API (v0), Parliamentary Counsel Office.

Docs: https://api.legislation.govt.nz/docs/
Auth: X-Api-Key header. 10,000 req/day per key, 2,000 per 5 min per IP.

Scope note: this API returns metadata and format URLs, not legislative text.
The text URLs on www.legislation.govt.nz sit behind an AWS WAF JS challenge,
so server-side fetching of XML/PDF/HTML will return HTTP 202 with an empty
body. Talk to PCO if you need bulk text.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

BASE = "https://api.legislation.govt.nz"

LegislationType = Literal["act", "amendment_paper", "bill", "secondary_legislation"]
SortBy = Literal["title_asc", "title_desc", "year_asc", "year_desc", "most_recently_updated"]


class RateLimited(Exception):
    """Daily quota exhausted or IP burst limit tripped."""


@dataclass(slots=True)
class Version:
    version_id: str
    title: str
    is_latest: bool | None = None
    formats: dict[str, str] = field(default_factory=dict)

    @property
    def version_date(self) -> str:
        return self.version_id.split("_en_")[-1]

    @classmethod
    def parse(cls, d: dict[str, Any]) -> "Version":
        return cls(
            version_id=d["version_id"],
            title=d["title"],
            is_latest=d.get("is_latest_version"),
            formats={f["type"]: f["url"] for f in d.get("formats", [])},
        )


@dataclass(slots=True)
class Work:
    work_id: str
    title: str
    legislation_type: str
    status: str | None
    agencies: list[str]
    publisher: str | None
    latest_matching: Version | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def html_url(self) -> str | None:
        return self.latest_matching.formats.get("html") if self.latest_matching else None

    @classmethod
    def parse(cls, d: dict[str, Any]) -> "Work":
        lmv = d.get("latest_matching_version")
        return cls(
            work_id=d["work_id"],
            title=(lmv or {}).get("title", d.get("title", "")),
            legislation_type=d.get("legislation_type", ""),
            status=d.get("act_status")
            or d.get("instrument_status")
            or d.get("bill_status")
            or d.get("legislation_status"),
            agencies=d.get("administering_agencies") or [],
            publisher=d.get("publisher"),
            latest_matching=Version.parse(lmv) if lmv else None,
            raw=d,
        )


class NZLegislation:
    def __init__(self, api_key: str, *, timeout: int = 40, min_interval: float = 0.12):
        self.api_key = api_key
        self.timeout = timeout
        self.min_interval = min_interval
        self.remaining: int | None = None
        self._last_call = 0.0

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        url = f"{BASE}{path}?" + urllib.parse.urlencode(params)

        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

        req = urllib.request.Request(url, headers={"X-Api-Key": self.api_key})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self._last_call = time.monotonic()
                    hdr = resp.headers.get("X-RateLimit-Remaining")
                    if hdr:
                        self.remaining = int(hdr)
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code in (429, 403):
                    raise RateLimited(f"{e.code} on {path}") from e
                if e.code >= 500 and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise
        raise RuntimeError("unreachable")

    # ---- endpoints -------------------------------------------------------

    def search(
        self,
        search_term: str | None = None,
        *,
        search_field: Literal["title", "content"] = "title",
        legislation_type: LegislationType | None = None,
        act_status: Literal["in_force", "not_in_force", "repealed"] | None = None,
        act_classification: Literal["principal", "amendment"] | None = None,
        act_type: str | None = None,
        bill_status: Literal["current", "enacted", "terminated"] | None = None,
        bill_type: str | None = None,
        instrument_status: str | None = None,
        instrument_type_group: str | None = None,
        instrument_classification: str | None = None,
        legislation_status: Literal["in_force", "not_in_force", "no_value"] | None = None,
        administering_agencies: str | None = None,
        publisher: Literal["Agency", "Parliamentary Counsel Office"] | None = None,
        sort_by: SortBy | None = None,
        page: int = 1,
        per_page: int = 20,
    ) -> tuple[list[Work], int]:
        """One page of results. Returns (works, total_matches)."""
        d = self._get(
            "/v0/works/",
            search_term=search_term,
            search_field=search_field if search_term else None,
            legislation_type=legislation_type,
            act_status=act_status,
            act_classification=act_classification,
            act_type=act_type,
            bill_status=bill_status,
            bill_type=bill_type,
            instrument_status=instrument_status,
            instrument_type_group=instrument_type_group,
            instrument_classification=instrument_classification,
            legislation_status=legislation_status,
            administering_agencies=administering_agencies,
            publisher=publisher,
            sort_by=sort_by,
            page=page,
            per_page=min(per_page, 100),
        )
        return [Work.parse(r) for r in d["results"]], d["total"]

    def iter_search(self, *, limit: int | None = None, **kwargs: Any) -> Iterator[Work]:
        """Auto-paginating search. Caps at 100 per request."""
        kwargs.pop("page", None)
        kwargs["per_page"] = 100
        seen, page = 0, 1
        while True:
            works, total = self.search(page=page, **kwargs)
            if not works:
                return
            for w in works:
                yield w
                seen += 1
                if limit and seen >= limit:
                    return
            if seen >= total:
                return
            page += 1

    def versions(self, work_id: str, *, sort: Literal["asc", "desc"] = "desc") -> list[Version]:
        d = self._get(f"/v0/works/{work_id}/versions/", sort=sort)
        return [
            Version(
                version_id=r["version_id"],
                title=r["title"],
                formats={f["type"]: f["url"] for f in r.get("formats", [])},
            )
            for r in d["results"]
        ]

    def version(self, version_id: str) -> Version:
        return Version.parse(self._get(f"/v0/versions/{version_id}/"))


# ---- change monitoring ---------------------------------------------------


def check_watchlist(
    client: NZLegislation, work_ids: list[str], state_path: str | Path
) -> list[dict[str, str]]:
    """
    Compare the current latest version of each watched work against a stored
    snapshot. Returns a list of changes and rewrites the snapshot.

    Run this daily. One request per work, so a 200-work watchlist costs 200
    of the 10,000 daily calls.
    """
    state_path = Path(state_path)
    previous: dict[str, str] = {}
    if state_path.exists():
        previous = json.loads(state_path.read_text())

    current: dict[str, str] = {}
    changes: list[dict[str, str]] = []

    for wid in work_ids:
        vers = client.versions(wid, sort="desc")
        if not vers:
            continue
        latest = vers[0]
        current[wid] = latest.version_id
        was = previous.get(wid)
        if was and was != latest.version_id:
            changes.append(
                {
                    "work_id": wid,
                    "title": latest.title,
                    "from": was.split("_en_")[-1],
                    "to": latest.version_date,
                    "url": latest.formats.get("html", ""),
                }
            )

    state_path.write_text(
        json.dumps({"_checked": datetime.now(timezone.utc).isoformat(), **current}, indent=1)
    )
    return changes


def horizon_scan(client: NZLegislation, keywords: list[str], limit: int = 40) -> list[Work]:
    """
    Bills currently before the House whose titles match any keyword.
    Useful for telling a client what is coming before it lands.
    """
    hits: dict[str, Work] = {}
    for kw in keywords:
        for w in client.iter_search(
            search_term=kw,
            search_field="title",
            legislation_type="bill",
            bill_status="current",
            sort_by="most_recently_updated",
            limit=limit,
        ):
            hits[w.work_id] = w
    return sorted(
        hits.values(),
        key=lambda w: w.latest_matching.version_date if w.latest_matching else "",
        reverse=True,
    )


if __name__ == "__main__":
    import os
    import sys

    key = os.environ.get("NZL_API_KEY")
    if not key:
        sys.exit("set NZL_API_KEY")

    c = NZLegislation(key)

    works, total = c.search("Health and Safety at Work", search_field="title",
                            legislation_type="act", act_status="in_force")
    print(f"{total} in-force Acts matching")
    for w in works[:5]:
        print(f"  {w.work_id:38} {w.title}")

    print("\nHorizon scan:")
    for w in horizon_scan(c, ["health and safety", "privacy", "employment"]):
        v = w.latest_matching
        print(f"  {v.version_date if v else '':12} {w.title}")

    print(f"\nquota remaining: {c.remaining}")