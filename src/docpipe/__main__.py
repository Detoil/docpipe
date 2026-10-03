"""Allow `python -m docpipe`, which is how the double-click launchers run it."""

from .cli import main

if __name__ == "__main__":
    main()
