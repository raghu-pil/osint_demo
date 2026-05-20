"""
OSINT Tool — Social media intelligence library.

Quick usage:
    from osint.main import run
    from osint.output import to_json

    report = run("https://twitter.com/user/status/123", config={})
    print(to_json(report))
"""
from .main import run
from .output import to_json, report_to_dict
