"""Launch the desktop or one of the standalone runtime's fixed worker modes."""

import multiprocessing

from veil.runtime import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
