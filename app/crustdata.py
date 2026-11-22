"""Crustdata client: retries on 429/timeouts, caches every response in SQLite
(so a restart never re-spends credits), and writes every call to the cost ledger."""
import hashlib
import json
import os
import time
from datetime import datetime, timezone

import httpx

BASE = "https://api.crustdata.com"
API_VERSION = "2025-11-01"


class SourceUnavailable(Exception):
    """The source did not answer. Never read silence as an answer: hold the case."""


class BudgetExceeded(Exception):
    pass


class Crustdata:
    def __init__(self, conn, run_id=None, budget=None, api_key=None, max_retries=4):
        self.conn = conn
        self.run_id = run_id
        self.budget = budget  # callable -> remaining credits, or None
        self.key = api_key or os.environ.get("CRUSTDATA_API_KEY")
        self.max_retries = max_retries
        if not self.key:
            raise RuntimeError("CRUSTDATA_API_KEY is not set (put it in .env)")

    def _ledger(self, invoice_id, endpoint, credits, cached, latency_ms, status):
        self.conn.execute(
            "INSERT INTO ledger(run_id, invoice_id, service, endpoint, credits, cached, latency_ms, status, at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (self.run_id, invoice_id, "crustdata", endpoint, credits, int(cached), latency_ms, status, time.time()),
        )

    def post(self, endpoint: str, body: dict, invoice_id=None):
        """Returns (json, retrieved_at_iso). Cached responses keep their original retrieval date."""
        cache_key = endpoint + ":" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        row = self.conn.execute("SELECT response, fetched_at FROM http_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if row:
            self._ledger(invoice_id, endpoint, 0.0, True, 0, "cache")
            return json.loads(row["response"]), row["fetched_at"]

        if self.budget is not None and self.budget() <= 0:
            raise BudgetExceeded(f"cost limit reached before calling {endpoint}")

        delay = 2.0
        last_err = None
        for attempt in range(self.max_retries):
            t0 = time.time()
            try:
                r = httpx.post(
                    BASE + endpoint,
                    json=body,
                    timeout=30,
                    headers={
                        "authorization": f"Bearer {self.key}",
                        "x-api-version": API_VERSION,
                        "content-type": "application/json",
                    },
                )
            except httpx.TransportError as e:  # timeout, DNS, reset
                last_err = f"transport: {e}"
                self._ledger(invoice_id, endpoint, 0.0, False, int((time.time() - t0) * 1000), "error")
                time.sleep(delay)
                delay *= 2
                continue
            latency = int((time.time() - t0) * 1000)
            credits = float(r.headers.get("x-credits-used", 0) or 0)
            self._ledger(invoice_id, endpoint, credits, False, latency, str(r.status_code))
            if r.status_code == 429:
                wait = float(r.headers.get("x-ratelimit-reset", delay))
                time.sleep(min(max(wait, 1.0), 60.0))
                continue
            if r.status_code >= 500:
                last_err = f"http {r.status_code}"
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 402:
                raise BudgetExceeded("Crustdata credits exhausted (402)")
            if r.status_code >= 400:
                raise SourceUnavailable(f"{endpoint} returned {r.status_code}: {r.text[:200]}")
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.conn.execute(
                "INSERT OR REPLACE INTO http_cache(cache_key, response, fetched_at) VALUES (?,?,?)",
                (cache_key, r.text, fetched_at),
            )
            return r.json(), fetched_at
        raise SourceUnavailable(f"{endpoint} failed after {self.max_retries} attempts ({last_err})")

    # ---- convenience wrappers -------------------------------------------------

    def identify_domain(self, domain: str, invoice_id=None):
        data, at = self.post("/company/identify", {"domains": [domain]}, invoice_id)
        matches = (data[0].get("matches") if data else None) or []
        return matches, at

    def identify_name(self, name: str, invoice_id=None):
        data, at = self.post("/company/identify", {"names": [name]}, invoice_id)
        matches = (data[0].get("matches") if data else None) or []
        return matches, at
