"""Command-line validation for the durable Radsync state branch."""
import argparse
import sys

from sources.state import validate_state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    valid, issues = validate_state(args.state_dir)
    for issue in issues:
        print(issue)
    if not valid:
        return 1
    print("Provider snapshot state is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
