import argparse
import json
import sys

from . import __version__
from . import config


def main():
    parser = argparse.ArgumentParser(description="Fleetlight Linux desktop fleet dashboard")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="Use an alternate private configuration file")
    parser.add_argument("--check", action="store_true", help="Print a read-only fleet snapshot as JSON and exit")
    parser.add_argument("--demo", action="store_true", help="Show fictional data, without network access")
    args = parser.parse_args()
    if args.check:
        from .monitor import refresh
        result = []
        try:
            settings = config.load(args.config)
            refresh(settings["hosts"], result.append)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(sorted(result, key=lambda item: item["id"]), indent=2))
        return 0 if all(item.get("status") == "online" for item in result) else 1
    try:
        from .app import Fleetlight
    except (ImportError, ValueError) as error:
        print("Fleetlight requires GTK4, libadwaita and PyGObject. See README installation instructions.\n" + str(error), file=sys.stderr)
        return 2
    return Fleetlight(args.config, demo=args.demo).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
