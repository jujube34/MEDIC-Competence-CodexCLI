"""Create a release manifest; refuse overwriting an existing release candidate."""
import argparse
import json
from pathlib import Path

from medic.runtime import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(build.MANIFEST))
    args = parser.parse_args()
    with Path(args.output).open('x', encoding='utf-8') as output:
        json.dump({'runtime_version': build.VERSION, 'files': build.inventory()}, output, indent=2, ensure_ascii=False)
        output.write('\n')


if __name__ == '__main__':
    main()
