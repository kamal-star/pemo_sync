# Copyright (c) 2026, Designsmith and contributors
# For license information, please see license.txt
"""
Thin client for the Pemo External API (v1).

Facts from Pemo's OpenAPI spec (v1.2.0) that shape this code:
  - Auth is one header, `apiKey`. Keys are bound to an environment: a sandbox
    key does not work against production.
  - Listings are page based, `limit` is capped at 50, and there is NO total
    count: keep paging until a page comes back shorter than the limit asked for.
  - Records are newest first, so `endDate` must be pinned for the whole run or
    new spend can shift rows between pages.
  - 429 carries an optional Retry-After; 429 and 5xx are retryable, other 4xx
    are terminal.
  - PATCH markAsExported is idempotent, so a retried confirmation is safe.
  - Receipt links are signed CloudFront URLs that expire ~30 minutes after the
    listing response, so they are downloaded page by page, straight away.
"""

import time

import frappe
import requests
from frappe import _

HOSTS = {
    "Production": "https://external-api.pemo.io/v1",
    "Sandbox": "https://external-api-sandbox.pemo.io/v1",
}
PAGE_SIZE = 50
MAX_PAGES = 2000
TIMEOUT = 60
MAX_RETRIES = 5
MAX_BACKOFF = 60
PATCH_CHUNK = 50


class PemoAPIError(Exception):
    pass


class PemoClient:
    def __init__(self, settings=None):
        settings = settings or frappe.get_single("Pemo Settings")
        self.base_url = HOSTS.get(settings.environment or "Production", HOSTS["Production"])
        key = settings.get_password("api_key", raise_exception=False)
        if not key:
            frappe.throw(_("Set the API Key in Pemo Settings first."))
        self.session = requests.Session()
        self.session.headers.update({"apiKey": key, "Accept": "application/json"})

    # ------------------------------------------------------------------ #
    # Transactions
    # ------------------------------------------------------------------ #
    def iter_transaction_pages(self, start_iso, end_iso, export_status="readyToExport"):
        """Yield one page (a list of transaction dicts) at a time.

        Yielding per page lets the caller download that page's receipts while
        their signed links are still fresh.
        """
        page = 1
        while page <= MAX_PAGES:
            data = self._request(
                "GET",
                "/transactions",
                params={
                    "startDate": start_iso,
                    "endDate": end_iso,
                    "exportStatus": export_status,
                    "page": page,
                    "limit": PAGE_SIZE,
                },
            )
            rows = (data or {}).get("transactions") or []
            yield rows
            if len(rows) < PAGE_SIZE:
                return
            page += 1
        raise PemoAPIError(_("Pemo returned more than {0} pages; narrow the date range.").format(MAX_PAGES))

    def mark_transactions_exported(self, transaction_ids):
        ids = [i for i in dict.fromkeys(transaction_ids) if i]
        for start in range(0, len(ids), PATCH_CHUNK):
            self._request(
                "PATCH",
                "/transactions",
                json={"operation": "markAsExported", "transactionIds": ids[start : start + PATCH_CHUNK]},
            )
        return len(ids)

    # ------------------------------------------------------------------ #
    # Receipts
    # ------------------------------------------------------------------ #
    @staticmethod
    def download(url):
        """Fetch a signed receipt URL. Returns (bytes, content_type).

        Deliberately NOT sent through self.session: the apiKey header must not
        leak to the CDN.
        """
        response = requests.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        if not response.content:
            raise PemoAPIError(_("Receipt download returned an empty file."))
        return response.content, (response.headers.get("content-type") or "").split(";")[0].strip()

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #
    def _request(self, method, path, **kwargs):
        url = self.base_url + path
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
            except requests.RequestException as e:
                last_error = str(e)
                time.sleep(min(2**attempt, MAX_BACKOFF))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                time.sleep(_retry_after(response, attempt))
                continue

            if response.status_code >= 400:
                raise PemoAPIError(
                    _("Pemo {0} {1} failed with HTTP {2}: {3}").format(
                        method, path, response.status_code, _error_message(response)
                    )
                )
            return response.json() if response.content else {}

        raise PemoAPIError(
            _("Pemo {0} {1} still failing after {2} attempts: {3}").format(method, path, MAX_RETRIES, last_error)
        )


def _retry_after(response, attempt):
    try:
        return min(int(response.headers.get("Retry-After")), MAX_BACKOFF)
    except (TypeError, ValueError):
        return min(2**attempt, MAX_BACKOFF)


def _error_message(response):
    try:
        body = response.json()
        return body.get("message") or body.get("code") or response.text[:300]
    except ValueError:
        return response.text[:300]
