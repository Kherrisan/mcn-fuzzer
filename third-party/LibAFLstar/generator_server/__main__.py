import argparse

from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="LibAFLstar generator server")
    parser.add_argument("--service", default="libaflstar/generator")
    args = parser.parse_args()
    serve(args.service)


if __name__ == "__main__":
    main()
