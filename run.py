import os
os.environ.setdefault("WEB_ALWAYS", "1")
import weblayer
import asyncio
import main

if __name__ == "__main__":
    asyncio.run(main.main())
