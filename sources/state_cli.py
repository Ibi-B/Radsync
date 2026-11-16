"""Command-line validation and retention-aware migration for durable Radsync state."""
import argparse
import json
import sys

from sources.state import validate_state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "validate",
            "restore-external",
            "publish-external",
            "fetch-external-report",
            "finalize-external",
            "prune-external-archive",
        ),
    )
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--config", default="config/sources.json")
    parser.add_argument("--report", default="state/diagnostics/source-report.json")
    parser.add_argument("--ambiguity", default="state/diagnostics/identity-ambiguity.json")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--max-age-days", type=int, default=90)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.command == "validate":
        valid, issues = validate_state(args.state_dir)
        for issue in issues:
            print(issue)
        if not valid:
            return 1
        print("Provider snapshot state is valid.")
        return 0

    from sources.retention import (
        fetch_external_report,
        finalize_external_rotation,
        prune_external_archive_objects,
        publish_external_snapshots,
        restore_external_snapshots,
    )

    try:
        if args.command == "restore-external":
            result = restore_external_snapshots(args.state_dir, args.config)
        elif args.command == "fetch-external-report":
            if not args.run_id or not args.output:
                parser.error("fetch-external-report requires --run-id and --output")
            result = fetch_external_report(args.state_dir, args.run_id, args.output)
        elif args.command == "finalize-external":
            result = finalize_external_rotation(args.state_dir)
        elif args.command == "prune-external-archive":
            result = prune_external_archive_objects(args.max_age_days, apply=args.apply)
        else:
            result = publish_external_snapshots(
                args.state_dir,
                args.config,
                args.report,
                args.run_id,
                args.ambiguity,
            )
    except Exception as error:
        print(f"External provider state operation failed: {type(error).__name__}: {error}")
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
