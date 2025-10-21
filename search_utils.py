from ddgs import DDGS
from main import logger

def ddg_search(query: str):
    try:
        with DDGS(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as dd:
            return list(dd.text(query, max_results=5))
    except Exception as e:
        logger.warning("DDGS failed: %s", e)
        return []
