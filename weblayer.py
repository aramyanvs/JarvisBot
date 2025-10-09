import os, re, httpx
from urllib.parse import urlparse
DEFAULT_HEADERS={"User-Agent":"Mozilla/5.0"}

def _to_jina(url:str)->str:
    u=urlparse(url)
    if not u.scheme:
        url="http://"+url
    return "https://r.jina.ai/http/"+url.replace("https://","").replace("http://","")

async def _fetch_httpx(url:str, limit:int=20000)->str:
    async with httpx.AsyncClient(follow_redirects=True, headers=DEFAULT_HEADERS, timeout=20) as cl:
        r=await cl.get(url)
        r.raise_for_status()
        t=r.text or ""
        return t[:limit]

async def fetch_via_jina(url:str, limit:int=20000)->str:
    j=_to_jina(url)
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as cl:
        r=await cl.get(j)
        r.raise_for_status()
        t=r.text or ""
        return t[:limit]

def apply():
    import asyncio
    import main as m
    async def new_fetch_url(url:str, limit:int=20000)->str:
        try:
            return await _fetch_httpx(url, limit)
        except:
            return await fetch_via_jina(url, limit)
    async def new_fetch_urls(urls, limit_chars:int=12000)->str:
        out=[]
        for u in urls[:3]:
            try:
                t=await new_fetch_url(u, 4000)
                if t: out.append(t)
            except:
                pass
        return ("\n\n".join(out))[:limit_chars]
    async def new_search_and_fetch(query:str, hits:int=3, limit_chars:int=12000)->str:
        links=[]
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddg:
                for r in ddg.text(query, max_results=hits, safesearch="moderate"):
                    if r and r.get("href"):
                        href=r["href"]
                        if href not in links:
                            links.append(href)
        except:
            links=[]
        return await new_fetch_urls(links, limit_chars) if links else ""
    def new_need_web(q:str)->bool:
        return True
    m.fetch_url=new_fetch_url
    m.fetch_urls=new_fetch_urls
    m.search_and_fetch=new_search_and_fetch
    m.need_web=new_need_web
