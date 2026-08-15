import sys

from . import toes
from . import __version__


USAGE = """usage: python3 -m feetbrowser [options] [url]

FeetBrowser: a functional web browser built from scratch.

options:
  -h, --help       show this help message and exit
  -v, --version    print the version and exit
  --screenshot <url> [out.png]
                   render <url> headlessly and write a PNG, then exit
                   (default: feetbrowser.png)
  --toes           list installed toes and their status
  --toe-search <term>      search the ToeHub catalog
  --toe-install <name>     install a toe from the catalog
  --toe-uninstall <name>   uninstall a toe
  --toe-enable <name>      enable a disabled toe
  --toe-disable <name>     disable an installed toe
  --new-toe <name>         scaffold a new toe
  --toe-docs               print the Toes documentation
  --to-bf <file>           compile the file's contents to Brainfuck and print
                           the program (use - for stdin)

If no URL is given the browser opens the welcome page.
"""


def main():
    args = sys.argv[1:]
    if not args:
        from .browser import main as browser_main
        sys.exit(browser_main() or 0)
    flag = args[0]
    if flag == "-h" or flag == "--help":
        print(USAGE)
        return
    if flag == "-v" or flag == "--version":
        print(f"FeetBrowser {__version__}")
        return
    if flag == "--toes":
        toes.list_toes()
        return
    if flag == "--toe-search":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-search <term>")
            sys.exit(1)
        toes.search_toes(args[1])
        return
    if flag == "--toe-install":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-install <name>")
            sys.exit(1)
        toes.install_toe(args[1])
        return
    if flag == "--toe-uninstall":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --toe-uninstall <name>")
            sys.exit(1)
        toes.uninstall_toe(args[1])
        return
    if flag == "--toe-enable" or flag == "--toe-disable":
        if len(args) < 2:
            print(f"usage: python3 -m feetbrowser {flag} <name>")
            sys.exit(1)
        sys.exit(toes.set_enabled(args[1], flag == "--toe-enable"))
    if flag == "--new-toe":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --new-toe <name>")
            sys.exit(1)
        sys.exit(toes.new_toe(args[1]))
    if flag == "--toe-docs":
        toes.toe_docs()
        return
    if flag == "--to-bf":
        if len(args) < 2:
            print("usage: python3 -m feetbrowser --to-bf <file>")
            sys.exit(1)
        from .brainfuck import compile
        path = args[1]
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        print(compile(text))
        return
    # Anything else is a URL passed to the browser.
    from .browser import main as browser_main
    sys.exit(browser_main() or 0)


if __name__ == "__main__":
    main()
