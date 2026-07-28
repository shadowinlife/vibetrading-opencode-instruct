"""Tushare API permission probe for escape-top validation external sources.

Probes 11 endpoints by requesting 1 row each. Classifies status as:
  - 'available': API returned data
  - 'permission_denied': HTTP/auth/permission error
  - 'api_error': timeout, network, unexpected error

Token read from .env via python-dotenv or os.environ. NEVER printed/logged.
"""

import json
import os
import sys
from datetime import datetime, timezone


def load_token() -> str:
    """Load TUSHARE_TOKEN from .env or environment, never print it."""
    token = os.getenv("TUSHARE_TOKEN")
    if token:
        return token
    try:
        from dotenv import load_dotenv
        load_dotenv()
        token = os.getenv("TUSHARE_TOKEN")
        if token:
            return token
    except ImportError:
        pass
    print("ERROR: TUSHARE_TOKEN not found in environment or .env", file=sys.stderr)
    sys.exit(1)


def probe_endpoint(pro, endpoint: str, method: str = "query", **kwargs) -> dict:
    """Probe a single Tushare endpoint with 1-row request.

    Returns dict with status, error (if any), and sample fields.
    """
    result = {
        "endpoint": endpoint,
        "method": method,
        "status": "unprobed",
        "error": None,
        "sample_row_count": 0,
        "sample_fields": [],
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        if method == "query":
            params = {"limit": 1}
            params.update(kwargs)
            df = pro.query(endpoint, **params)
        else:
            params = {"limit": 1}
            params.update(kwargs)
            fn = getattr(pro, method, None)
            if fn is None:
                result["status"] = "api_error"
                result["error"] = f"Unknown method: {method}"
                return result
            df = fn(**params)

        if df is None or len(df) == 0:
            result["status"] = "available"
            result["error"] = "empty_result"
            result["sample_row_count"] = 0
        else:
            result["status"] = "available"
            result["sample_row_count"] = len(df)
            result["sample_fields"] = list(df.columns)
    except Exception as exc:
        msg = str(exc)
        if any(kw in msg.lower() for kw in
               ["权限", "permission", "unauthorized", "积分", "access denied",
                "forbidden", "not allowed", "not authorized"]):
            result["status"] = "permission_denied"
        else:
            result["status"] = "api_error"
        result["error"] = msg[:500]

    return result


ENDPOINTS = [
    {"endpoint": "moneyflow_hsgt", "method": "query", "hint": "Northbound flow"},
    {"endpoint": "index_dailybasic", "method": "query", "kwargs": {"ts_code": "000001.SH"}, "hint": "Index daily basic"},
    {"endpoint": "daily_info", "method": "query", "kwargs": {"ts_code": "000001.SZ"}, "hint": "Stock daily info"},
    {"endpoint": "etf_share_size", "method": "query", "kwargs": {"ts_code": "510050.SH"}, "hint": "ETF share size"},
    {"endpoint": "fund_basic", "method": "query", "kwargs": {"market": "E"}, "hint": "Fund basic info"},
    {"endpoint": "fund_nav", "method": "query", "kwargs": {"ts_code": "000001.OF"}, "hint": "Fund NAV"},
    {"endpoint": "shibor", "method": "query", "hint": "Shibor rates"},
    {"endpoint": "shibor_lpr", "method": "query", "hint": "LPR rates"},
    {"endpoint": "cn_m", "method": "query", "hint": "Money supply"},
    {"endpoint": "cn_social_financing", "method": "query", "hint": "Social financing"},
    {"endpoint": "opt_daily", "method": "query", "kwargs": {"ts_code": "510050.SH"}, "hint": "Options daily"},
]


def run_all_probes(token: str) -> dict:
    """Probe all 11 required endpoints."""
    import tushare as ts
    pro = ts.pro_api(token)

    results = []
    for ep in ENDPOINTS:
        endpoint = ep["endpoint"]
        method = ep.get("method", "query")
        kwargs = ep.get("kwargs", {})
        hint = ep.get("hint", "")
        sys.stderr.write(f"Probing {endpoint} ({hint})... ")
        r = probe_endpoint(pro, endpoint, method, **kwargs)
        status = r["status"]
        if status == "available":
            n = r["sample_row_count"]
            sys.stderr.write(f"{status} ({n} rows)\n")
        else:
            sys.stderr.write(f"{status}: {r['error'][:80]}\n")
        results.append(r)

    blocked = [r for r in results if r["status"] == "permission_denied"]
    errored = [r for r in results if r["status"] == "api_error"]
    available = [r for r in results if r["status"] == "available"]

    report = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "framework": "escape-top-microstructure-validation",
            "total_endpoints": len(ENDPOINTS),
        },
        "summary": {
            "available": len(available),
            "permission_denied": len(blocked),
            "api_error": len(errored),
            "human_action_required": len(blocked) > 0,
        },
        "results": results,
    }

    return report


def save_report(report: dict, output_path: str):
    """Write probe report to JSON file."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def generate_hitl_requests(report: dict, output_path: str):
    """Generate human-in-the-loop request doc for blocked endpoints."""
    blocked = [r for r in report["results"] if r["status"] == "permission_denied"]
    if not blocked:
        return

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    lines = [
        "# Escape-Top Validation: Data Permission Procurement Request",
        "",
        f"**Generated**: {report['meta']['generated_at']}",
        f"**Framework**: {report['meta']['framework']}",
        f"**Endpoints needing permission**: {len(blocked)} of {report['meta']['total_endpoints']}",
        "",
        "## Blocked Endpoints",
        "",
        "The following Tushare endpoints returned `permission_denied` and are currently unavailable ",
        "for the escape-top microstructure validation framework:",
        "",
    ]

    for i, r in enumerate(blocked, 1):
        ep = r["endpoint"]
        err = r.get("error", "Unknown")
        lines.append(f"### {i}. `{ep}`")
        lines.append(f"- **Method**: `{r['method']}`")
        lines.append(f"- **Error**: `{err}`")
        lines.append(f"- **Probed at**: {r['probed_at']}")
        lines.append("")

    lines.extend([
        "## Action Required",
        "",
        "To proceed with the escape-top validation framework, the following data is needed:",
        "",
    ])
    for r in blocked:
        ep = r["endpoint"]
        lines.append(f"- **`{ep}`**: need Tushare point upgrade or permission grant")
    lines.extend([
        "",
        "## How to Resolve",
        "",
        "1. Log into https://tushare.pro and check the current account tier / points.",
        "2. Upgrade the account or purchase points if needed.",
        "3. After upgrade, re-run the probe:",
        "   ```bash",
        "   conda activate legonanobot",
        "   python scripts/microstructure/tushare_probe.py",
        "   ```",
        "4. If resolved, the `blocked-by-permission` status in the probe report will change to `available`.",
        "",
        "## Impact",
        "",
        "Without these endpoints, the escape-top framework lacks:",
        "- Northbound capital flow data (moneyflow_hsgt) — cross-market sentiment",
        "- Index-level valuation metrics (index_dailybasic) — PE/PB at index level",
        "- Individual stock daily indicators (daily_info) — complementary breadth metrics",
        "- ETF flow analysis (etf_share_size, fund_basic, fund_nav) — smart-money flow proxy",
        "- Macro liquidity indicators (shibor, shibor_lpr, cn_m, cn_social_financing) — macro regime overlay",
        "- Options market data (opt_daily) — implied volatility for risk sentiment",
        "",
        "The core escape-top signal (成交集中度 + 两融背离) can still be computed from local DuckDB data ",
        "without these external endpoints, but the enrichment modules will be limited.",
    ])

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    sys.stderr.write(f"HITL request written to {output_path}\n")


if __name__ == "__main__":
    output = sys.argv[1] if len(sys.argv) > 1 else "tmp/microstructure/tushare_probe_report.json"
    hitl_path = "tmp/microstructure/hitl_requests.md"

    token = load_token()
    report = run_all_probes(token)
    save_report(report, output)

    print(f"Probe complete: {report['summary']}")
    print(f"Report: {output}")

    if report["summary"]["human_action_required"]:
        generate_hitl_requests(report, hitl_path)
        print(f"HITL requests: {hitl_path}")
        sys.exit(2)  # non-zero for permission failures
    else:
        print("All endpoints available. No HITL needed.")