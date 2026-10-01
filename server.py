import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import PlainTextResponse
from checker import check_site, summarize, CheckError

mcp = FastMCP(
    "Pillar AI Visibility Checker",
    instructions="Checks whether a public website is technically ready to be found and cited by AI search tools. It inspects one page, robots.txt and llms.txt. It does not measure rankings or AI mentions.",
    host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
    stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

@mcp.tool(title="Check AI visibility readiness", annotations=RO,
          description="Use when the user wants to know whether a public website is set up to be found by AI search and answer engines (ChatGPT search, Perplexity, Google AI features). Fetches one public page plus robots.txt and llms.txt and returns a pass/fix checklist: title, meta description, H1, canonical, noindex, Open Graph, JSON-LD, text in initial HTML, and which AI crawlers robots.txt allows or blocks. Do not use it to predict rankings or measure whether AI tools mention a brand.")
def check_ai_visibility_readiness(url: str) -> dict:
    """url: public website address, for example example.com or https://example.com/pricing"""
    try:
        r = check_site(url)
    except CheckError as e:
        return {"error": str(e)}
    r["summary"] = summarize(r)
    return r

@mcp.tool(title="List AI crawler names", annotations=RO,
          description="Use when the user asks which AI crawlers and user agents exist and what each is for (GPTBot, OAI-SearchBot, ChatGPT-User, PerplexityBot, ClaudeBot, Google-Extended, CCBot). Static reference, no network access.")
def list_ai_crawlers() -> dict:
    from checker import AI_BOTS
    return {"crawlers": [{"user_agent": k, "purpose": v} for k, v in AI_BOTS.items()],
            "note": "Blocking a training crawler does not necessarily block the search crawler of the same company. Check each user agent separately."}

@mcp.custom_route("/", methods=["GET"])
async def home(request):
    return PlainTextResponse("Pillar AI Visibility Checker. MCP endpoint: /mcp\n")

@mcp.custom_route("/healthz", methods=["GET"])
async def health(request):
    return PlainTextResponse("ok")

app = mcp.streamable_http_app()
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
