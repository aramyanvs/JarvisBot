import os, asyncio

try:
    import weblayer
    weblayer.install()
except Exception:
    pass

import main

if __name__ == "__main__":
    if hasattr(main, "main") and asyncio.iscoroutinefunction(main.main):
        asyncio.run(main.main())
    elif hasattr(main, "run"):
        main.run()
    else:
        raise SystemExit("main.py does not expose main() or run()")
