"""Compare rules and RAG diagnosis for the bundled incident examples."""

import argparse
import json
import sys
from http.client import HTTPException
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
EXAMPLES = ("crashloopbackoff.json", "imagepullbackoff.json", "oomkilled.json")


class ComparisonError(Exception):
    """A diagnosis request could not be used in the comparison."""


def request_report(endpoint: str, mode: Literal["rules", "rag"], payload: bytes) -> dict[str, Any]:
    try:
        request = Request(
            f"{endpoint}?mode={mode}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            report = json.load(response)
    except HTTPError as error:
        error.close()
        raise ComparisonError(f"{mode} request returned HTTP {error.code}.") from None
    except TimeoutError:
        raise ComparisonError(f"{mode} request timed out.") from None
    except (HTTPException, OSError):
        raise ComparisonError(f"{mode} request failed. Check the API URL and server.") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ComparisonError(f"{mode} response was not valid JSON.") from None
    except ValueError:
        raise ComparisonError("Invalid API URL. Use an HTTP or HTTPS URL.") from None

    expected_method = "rules+rag" if mode == "rag" else "rules"
    if not isinstance(report, dict) or report.get("diagnosis_method") != expected_method:
        raise ComparisonError(
            f"{mode} response did not report diagnosis_method={expected_method}. "
            "Check that the Phase 2 API is running."
        )
    return report


def print_report(report: dict[str, Any]) -> None:
    failure = report["failure_type"] or "unclassified"
    print(f"  {report['diagnosis_method']}: {failure} ({report['status']})")
    for reference in report["references"]:
        print(f"    - {reference['title']} ({reference['url']})")
        if chunk_id := reference.get("chunk_id"):
            excerpt = " ".join(reference["excerpt"].split())
            print(f"      {chunk_id}: {excerpt}")


def print_comparison(name: str, rules: dict[str, Any], rag: dict[str, Any]) -> None:
    preserved = all(rules[field] == rag[field] for field in ("status", "failure_type"))
    added_steps = [
        step for step in rag["recommended_steps"] if step not in rules["recommended_steps"]
    ]

    print(name)
    print_report(rules)
    print_report(rag)
    print(f"  Classification preserved: {'yes' if preserved else 'no'}")
    print("  Source-backed steps added:")
    for step in added_steps or ["(none)"]:
        print(f"    - {step}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Diagnosis server base URL (default: %(default)s).",
    )
    args = parser.parse_args()
    if not args.base_url.startswith(("http://", "https://")):
        parser.error("--base-url must start with http:// or https://")
    endpoint = f"{args.base_url.rstrip('/')}/api/v1/diagnose"

    for name in EXAMPLES:
        try:
            payload = (EXAMPLES_DIR / name).read_bytes()
        except OSError:
            print(f"{name}: could not read the bundled example.", file=sys.stderr)
            return 1
        try:
            rules = request_report(endpoint, "rules", payload)
            rag = request_report(endpoint, "rag", payload)
            print_comparison(name, rules, rag)
        except ComparisonError as error:
            print(f"{name}: {error}", file=sys.stderr)
            return 1
        except (AttributeError, KeyError, TypeError):
            print(f"{name}: the API response has unexpected diagnosis fields.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    raise SystemExit(main())
