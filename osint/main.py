"""
OSINT Tool — main entry point.

Usage:
    python -m osint.main <social_media_url> [options]
    python -m osint.main --help
"""
import sys
import argparse
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .core.url_parser import parse_url
from .core.utils import make_session
from .core.models import OSINTReport
from .scrapers import get_scraper
from .intelligence.searxng import SearXNGClient
from .intelligence.sherlock_runner import run_sherlock
from .intelligence.crosspost import detect_crossposts
from .intelligence.darkweb import gather_darkweb_intel
from .analysis.account import analyse_account, analyse_posting_patterns
from .analysis.content import analyse_content, detect_language
from .analysis.redflags import darkweb_flags, network_flags
from .output.formatter import to_json


def load_config(config_path: Optional[str] = None) -> dict:
    paths = [
        config_path,
        "osint/config.yaml",
        "config.yaml",
        str(Path.home() / ".osint" / "config.yaml"),
    ]
    for p in paths:
        if p and Path(p).exists():
            try:
                import yaml
                with open(p) as f:
                    cfg = yaml.safe_load(f) or {}
                logging.info("Loaded config from %s", p)
                return cfg
            except Exception as e:
                logging.warning("Config load failed (%s): %s", p, e)
    return {}


def run(url: str, config: dict, verbose: bool = False) -> OSINTReport:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        # Suppress retry/connection warnings — expected when external services
        # are firewalled or unreachable; they clutter the output with no action.
        for noisy in ("urllib3.connectionpool", "urllib3.util.retry", "osint.core.utils",
                      "osint.scrapers.twitter", "osint.intelligence"):
            logging.getLogger(noisy).setLevel(logging.ERROR)

    parsed = parse_url(url)
    report = OSINTReport(
        input_url=url,
        generated_at=datetime.now(tz=timezone.utc),
        platform=parsed.platform,
    )

    session = make_session()
    searxng: Optional[SearXNGClient] = None
    if config.get("searxng_url"):
        searxng = SearXNGClient(config["searxng_url"], session=session)

    # ------------------------------------------------------------------ #
    #  1. Scrape the post                                                  #
    # ------------------------------------------------------------------ #
    scraper = get_scraper(parsed.platform, session=session, config=config)

    if scraper and parsed.post_id:
        try:
            # LinkedIn needs the original URL (slug can't be rebuilt from post_id alone)
            if parsed.platform == "linkedin" and hasattr(scraper, "get_post_from_url"):
                post = scraper.get_post_from_url(parsed.raw)
                account = scraper.get_account(parsed.username) if parsed.username else None
            else:
                post, account = scraper.get_post_and_account(parsed.post_id, parsed.username)
            report.post = post
            report.account = account
        except Exception as e:
            report.errors.append(f"Scraper error: {e}")

    # If we only have a profile URL (no post ID), fetch account only
    if scraper and not parsed.post_id and parsed.username and not report.account:
        try:
            report.account = scraper.get_account(parsed.username)
        except Exception as e:
            report.errors.append(f"Account scraper error: {e}")

    # ------------------------------------------------------------------ #
    #  2. Content + language analysis                                      #
    # ------------------------------------------------------------------ #
    if report.post:
        content_flags = analyse_content(report.post)
        report.red_flags.extend(content_flags)
        if not report.post.language and report.post.text:
            report.post.language = detect_language(report.post.text)
            report.metadata["detected_language"] = report.post.language

    # ------------------------------------------------------------------ #
    #  3. Account analysis                                                 #
    # ------------------------------------------------------------------ #
    if report.account:
        acct_flags = analyse_account(report.account)
        report.red_flags.extend(acct_flags)
        if report.account.recent_posts:
            cadence_flags = analyse_posting_patterns(report.account.recent_posts)
            report.red_flags.extend(cadence_flags)

    # ------------------------------------------------------------------ #
    #  4. Cross-post / repost detection                                   #
    # ------------------------------------------------------------------ #
    if report.post:
        try:
            crossposts = detect_crossposts(report.post, session, searxng_client=searxng)
            report.cross_posts = crossposts
        except Exception as e:
            report.errors.append(f"Cross-post detection error: {e}")

    # ------------------------------------------------------------------ #
    #  5. Username discovery (Sherlock)                                   #
    # ------------------------------------------------------------------ #
    username = (report.account and report.account.username) or parsed.username
    if username and not config.get("skip_sherlock"):
        try:
            sherlock_results = run_sherlock(username, timeout=config.get("sherlock_timeout", 90))
            report.username_search = sherlock_results
        except Exception as e:
            report.errors.append(f"Sherlock error: {e}")

    # ------------------------------------------------------------------ #
    #  6. Dark web intelligence                                            #
    # ------------------------------------------------------------------ #
    if not config.get("skip_darkweb"):
        email = (report.account and report.account.email) or None
        display = (report.account and report.account.display_name) or None
        try:
            dw_result = gather_darkweb_intel(
                username=username,
                email=email,
                display_name=display,
                session=session,
                config=config,
                searxng_client=searxng,
            )
            report.darkweb_hits = dw_result["hits"]
            report.metadata["manual_searches"] = dw_result["manual_searches"]
        except Exception as e:
            report.errors.append(f"Dark web error: {e}")

    # ------------------------------------------------------------------ #
    #  7. Aggregate red flags from network / darkweb                      #
    # ------------------------------------------------------------------ #
    report.red_flags.extend(darkweb_flags(report.darkweb_hits or []))
    report.red_flags.extend(network_flags(report.cross_posts or [], report.username_search or []))

    # Sort: high → medium → low
    _severity_order = {"high": 0, "medium": 1, "low": 2}
    report.red_flags.sort(key=lambda f: _severity_order.get(f.severity, 3))

    return report


def main():
    parser = argparse.ArgumentParser(
        description="OSINT tool — analyse a social media URL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m osint.main "https://twitter.com/user/status/123456789"
  python -m osint.main "https://www.reddit.com/r/sub/comments/abc123/title/" --verbose
  python -m osint.main "https://www.tiktok.com/@user/video/123456" --config my_config.yaml --output report.json
        """,
    )
    parser.add_argument("url", help="Social media URL to investigate")
    parser.add_argument("--config", "-c", help="Path to config YAML file", default=None)
    parser.add_argument("--output", "-o", help="Write JSON output to this file (default: stdout)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--skip-sherlock", action="store_true", help="Skip Sherlock username search")
    parser.add_argument("--skip-darkweb", action="store_true", help="Skip dark web intelligence")
    parser.add_argument("--no-crossposts", action="store_true", help="Skip cross-post detection")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.skip_sherlock:
        config["skip_sherlock"] = True
    if args.skip_darkweb:
        config["skip_darkweb"] = True
    if args.no_crossposts:
        config["skip_crossposts"] = True

    try:
        report = run(args.url, config, verbose=args.verbose)
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback; traceback.print_exc()
        sys.exit(2)

    output = to_json(report)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
