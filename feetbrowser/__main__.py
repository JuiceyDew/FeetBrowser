import sys

from . import toes


def main():
    args = sys.argv[1:]
    if not args:
        from .browser import main as browser_main
        browser_main()
        return
    if args[0] == "--toes":
        toes.list_toes()
        return
    if args[0] == "--new-toe":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --new-toe <name>")
            sys.exit(1)
        sys.exit(toes.new_toe(args[1]))
    if args[0] == "--toe-docs":
        toes.toe_docs()
        return
    print("usage: python3 -m feetbrowser [--toes | --new-toe <name> | "
          "--toe-docs] [url]")
    sys.exit(1)


if __name__ == "__main__":
    main()
