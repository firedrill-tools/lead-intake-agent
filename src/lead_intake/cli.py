import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the lead intake webhook")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--list-cases", action="store_true", help="Validate/list bundled cases without model or connector calls")
    selection.add_argument("--init-case", metavar="CASE", help="Initialize a fresh synthetic SQLite case without model calls")
    parser.add_argument("--db", type=Path, help="SQLite file; required for --init-case; serving defaults to INTAKE_DB/state.sqlite")
    parser.add_argument("--request-out", type=Path, help="Copy the selected form to a new private JSON file during initialization")
    args = parser.parse_args(argv)
    if args.request_out is not None and not args.init_case:
        parser.error("--request-out requires --init-case")
    if args.list_cases or args.init_case:
        from .datasets import initialize_case, list_cases
        if args.init_case and args.db is None:
            parser.error("--init-case requires --db with a fresh path")
        try:
            result = list_cases() if args.list_cases else initialize_case(args.init_case, args.db, args.request_out)
        except (ValueError, OSError, KeyError) as error:
            parser.error(str(error))
        print(json.dumps(result))
        return 0
    import uvicorn
    from .service import create_app
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
