"""AI visibility readiness checks for a public website. Read-only, public pages only."""
import ipaddress, json, re, socket
from urllib.parse import urlparse, urljoin
import httpx
from bs4 import BeautifulSoup

UA = "PillarAIVisibilityChecker/1.0 (+https://pillar-agent-fabric.onrender.com/ai-visibility)"
MAX_BYTES = 1_000_000
AI_BOTS = {
    "GPTBot": "OpenAI training crawler",
    "OAI-SearchBot": "ChatGPT search index",
    "ChatGPT-User": "ChatGPT user-initiated browsing",
    "PerplexityBot": "Perplexity index",
    "ClaudeBot": "Anthropic crawler",
    "Google-Extended": "Gemini/AI training control token",
    "CCBot": "Common Crawl",
}

class CheckError(Exception):
    pass

def _safe_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise CheckError("Could not resolve that host.")
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if not ip.is_global:
            raise CheckError("Only public internet sites can be checked.")

def normalize(url):
    url = (url or "").strip()
    if not url:
        raise CheckError("Provide a website URL.")
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise CheckError("Only http(s) URLs are supported.")
    if p.username or p.password:
        raise CheckError("URLs with credentials are not supported.")
    if p.port not in (None, 80, 443):
        raise CheckError("Only standard ports are supported.")
    _safe_host(p.hostname)
    return url

def fetch(client, url, hops=4):
    for _ in range(hops + 1):
        url = normalize(url)
        with client.stream("GET", url, follow_redirects=False) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    buf = buf[:MAX_BYTES]
                    break
            return r.status_code, r.headers, buf.decode(r.encoding or "utf-8", "replace"), url
    raise CheckError("Too many redirects.")

def parse_robots(text):
    groups, cur, agents = {}, [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = [s.strip() for s in line.split(":", 1)]
        k = k.lower()
        if k == "user-agent":
            if agents is False:
                cur = []
            cur.append(v.lower()); agents = True
            groups.setdefault(v.lower(), [])
        elif k in ("allow", "disallow"):
            agents = False
            for a in cur:
                groups[a].append((k, v))
    return groups

def bot_status(groups, bot):
    rules = groups.get(bot.lower())
    src = bot
    if rules is None:
        rules = groups.get("*"); src = "*"
    if rules is None:
        return "allowed", "no matching rule"
    dis = [v for k, v in rules if k == "disallow" and v]
    if any(v == "/" for v in dis):
        if any(k == "allow" and v for k, v in rules):
            return "partly blocked", f"Disallow: / with Allow rules under {src}"
        return "blocked", f"Disallow: / under {src}"
    if dis:
        return "partly blocked", f"{len(dis)} disallowed path(s) under {src}"
    return "allowed", f"open under {src}"

def check_site(url):
    url = normalize(url)
    out = {"checked_url": url, "findings": [], "score_note": "Checklist count, not a ranking or a prediction of AI mentions."}
    with httpx.Client(headers={"User-Agent": UA}, timeout=10) as c:
        try:
            status, headers, html, final = fetch(c, url)
        except httpx.HTTPError:
            raise CheckError("The site did not respond.")
        origin = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
        out["final_url"] = final
        out["http_status"] = status
        F = out["findings"]
        def add(name, ok, detail):
            F.append({"check": name, "pass": ok, "detail": detail})
        add("Page loads", status == 200, f"HTTP {status}")
        if status != 200:
            out["passed"], out["total"] = 0, 1
            out["limits"] = f"The page returned HTTP {status} to this checker, so content checks were skipped. Some sites block automated requests."
            return out
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.string or "").strip() if soup.title else ""
        add("Title tag", 10 <= len(title) <= 70, f"{len(title)} chars: {title[:90]!r}" if title else "missing")
        md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        d = (md.get("content") or "").strip() if md else ""
        add("Meta description", 50 <= len(d) <= 170, f"{len(d)} chars" if d else "missing")
        h1 = soup.find_all("h1")
        add("Single H1", len(h1) == 1, f"{len(h1)} H1 element(s)")
        can = soup.find("link", rel="canonical")
        add("Canonical link", bool(can and can.get("href")), can.get("href") if can and can.get("href") else "missing")
        rb = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
        rbv = (rb.get("content") or "").lower() if rb else ""
        xr = headers.get("x-robots-tag", "").lower()
        noidx = "noindex" in rbv or "noindex" in xr
        add("Not marked noindex", not noidx, "noindex found" if noidx else "no noindex directive")
        og = [m for m in soup.find_all("meta", property=re.compile("^og:(title|description|image)$"))]
        add("Open Graph tags", len(og) >= 2, f"{len(og)} of og:title/description/image")
        types = set()
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "")
            except Exception:
                continue
            stack = data if isinstance(data, list) else [data]
            while stack:
                x = stack.pop()
                if isinstance(x, dict):
                    t = x.get("@type")
                    for tt in (t if isinstance(t, list) else [t]):
                        if isinstance(tt, str): types.add(tt)
                    stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
                elif isinstance(x, list):
                    stack.extend(x)
        add("Structured data (JSON-LD)", bool(types), ", ".join(sorted(types)[:8]) or "none found")
        words = len(soup.get_text(" ", strip=True).split())
        add("Text in initial HTML", words >= 150, f"about {words} words (pages that render only in JavaScript can look empty to crawlers)")
        # robots
        bots = []
        try:
            rs, _, rt, _ = fetch(c, origin + "/robots.txt")
        except Exception:
            rs, rt = None, ""
        if rs == 200 and "<html" not in rt[:300].lower():
            g = parse_robots(rt)
            for b, desc in AI_BOTS.items():
                st, why = bot_status(g, b)
                bots.append({"bot": b, "purpose": desc, "status": st, "why": why})
            add("robots.txt found", True, "present")
            blocked = [x["bot"] for x in bots if x["status"] == "blocked" and x["bot"] in ("OAI-SearchBot", "ChatGPT-User", "PerplexityBot")]
            add("Search-type AI crawlers not blocked", not blocked, "blocked: " + ", ".join(blocked) if blocked else "OAI-SearchBot, ChatGPT-User, PerplexityBot not fully blocked")
            add("Sitemap declared in robots.txt", bool(re.search(r"(?im)^\s*sitemap:", rt)), "")
        else:
            add("robots.txt found", False, f"status {rs}; with no file, crawlers are allowed by default")
        out["ai_crawler_access"] = bots
        try:
            ls, _, lt, _ = fetch(c, origin + "/llms.txt")
            has = ls == 200 and "<html" not in lt[:300].lower() and len(lt.strip()) > 20
        except Exception:
            has = False
        add("llms.txt present (optional, unproven benefit)", has, "found" if has else "not found")
    required = [f for f in F if not f["check"].startswith("llms.txt")]
    out["passed"] = sum(1 for f in required if f["pass"])
    out["total"] = len(required)
    out["limits"] = "Checks one page plus robots.txt and llms.txt. It does not query AI answer engines or measure citations."
    return out

def summarize(r):
    lines = [f"{r['checked_url']}: {r['passed']}/{r['total']} readiness checks passed (one page)."]
    for f in r["findings"]:
        lines.append(f"- {'PASS' if f['pass'] else 'FIX '} {f['check']}: {f['detail']}".rstrip(": "))
    if r.get("ai_crawler_access"):
        lines.append("AI crawler rules in robots.txt: " + "; ".join(f"{b['bot']} {b['status']}" for b in r["ai_crawler_access"]))
    lines.append(r["limits"])
    return "\n".join(lines)
