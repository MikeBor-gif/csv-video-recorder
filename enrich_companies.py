"""Standalone PostgreSQL -> Impressum -> DeepSeek enrichment (no Flask import)."""

import argparse
import logging
import os
import time

import psycopg2
import requests

import company_data
from company_data import CompanyStatus
from impressum import (DeepSeekError, EnrichmentConfigError, FetchError, extract_company_data,
                       extract_visible_text, fetch_html, find_impressum_url,
                       normalize_website_url, optimize_text)

logger = logging.getLogger(__name__)


def enrich_one(row, session, extractor=None):
    """Return only changes from this attempt; early failures preserve old fields."""
    changes = {}
    try:
        home_url = normalize_website_url(row["web"])
        if home_url is None:
            logger.info("no website id=%s", row["id"])
            return {"status": CompanyStatus.NO_WEBSITE}
        html, resolved_home = fetch_html(session, home_url)
        url = find_impressum_url(html, resolved_home)
        if not url:
            logger.info("no Impressum id=%s home=%s", row["id"], resolved_home)
            return {"status": CompanyStatus.NO_IMPRESSUM_LINK_FOUND}
        changes["impressum_url"] = url
        logger.debug("Impressum id=%s url=%s", row["id"], url)
        if url != resolved_home:
            html, url = fetch_html(session, url)
            changes["impressum_url"] = url
        text = extract_visible_text(html)
        optimized, metrics = optimize_text(text)
        logger.info("company=%s text_strategy=%s", row["company_name"], metrics["strategy"])
        changes.update((extractor or extract_company_data)(optimized, row["company_name"]))
    except (FetchError, DeepSeekError) as exc:
        logger.error("enrichment id=%s company=%s reason=%s", row["id"], row["company_name"], exc)
        changes["status"] = CompanyStatus.ERROR
    return changes


def run(config, company_name=None, limit=None, status=None, session=None, sleep=time.sleep):
    company_data.initialize(config)
    rows = company_data.select_for_enrichment(config, company_name, status, limit)
    counts = dict(selected=len(rows), saved=0, conflicted=0, failed=0)
    logger.info("batch selected=%d", len(rows))
    owned = session is None
    session = session or requests.Session()
    session.max_redirects = 5
    try:
        for index, row in enumerate(rows):
            if index:
                sleep(1)
            logger.info("processing id=%s company=%s", row["id"], row["company_name"])
            changes = enrich_one(row, session)
            try:
                saved = company_data.save_enrichment(config, row, changes)
            except psycopg2.Error as exc:
                logger.error("DB write failed id=%s category=%s", row["id"], type(exc).__name__)
                counts["failed"] += 1
                continue
            counts["saved" if saved else "conflicted"] += 1
            if saved:
                counts["failed"] += changes["status"] != CompanyStatus.OK
                logger.info("company=%s status=%d (%s)", row["company_name"], changes["status"],
                            CompanyStatus(changes["status"]).name)
    finally:
        if owned:
            session.close()
    logger.info("batch finished %s", counts)
    return counts


def _positive_int(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company")
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--status", type=int, choices=[int(s) for s in CompanyStatus])
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, os.environ.get("ENRICH_LOG_LEVEL", "DEBUG").upper(), logging.DEBUG),
                        format="%(levelname)s %(name)s: %(message)s")
    # Third-party DEBUG HTTP wire logs are unnecessary and may reveal headers.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        counts = run(company_data.db_config_from_env(), args.company, args.limit, args.status)
        return 1 if counts["failed"] or counts["conflicted"] else 0
    except psycopg2.Error as exc:
        logger.error("enrichment database unavailable: %s", type(exc).__name__)
        return 1
    except RuntimeError as exc:
        logger.error("enrichment cannot continue: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
