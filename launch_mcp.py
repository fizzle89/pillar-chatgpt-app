"""Launch-layer MCP server (private, bearer-token roles) + combined ASGI app."""
import contextvars, hmac, os
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse
from starlette.routing import Mount
import launch as L

ROLE = contextvars.ContextVar("role", default=None)
PUBLIC = ("/launch/", "/launch/healthz", "/launch/templates")

def _tok(name):
    return os.environ.get(name, "")

def role_for(header):
    if not header.lower().startswith("bearer "): return None
    t = header[7:].strip()
    for name, role in (("LAUNCH_OWNER_TOKEN", "owner"), ("LAUNCH_OPERATOR_TOKEN", "operator")):
        want = _tok(name)
        if want and len(want) >= 24 and hmac.compare_digest(t, want): return role
    return None

class Auth:
    """Fail closed: /launch/mcp needs a configured token. Public: info page and template list only."""
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/launch") and scope["path"] not in PUBLIC:
            hdr = dict(scope["headers"]).get(b"authorization", b"").decode()
            r = role_for(hdr)
            if not r:
                await JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send); return
            ROLE.set(r)
        await self.app(scope, receive, send)

lm = FastMCP("Pillar Launch Layer",
    instructions="Create, launch and run vertical agents with approvals, budgets, rubric review and versioned methods. Mutating tools need a bearer token. Ask-first agents always wait for owner approval, including jobs started over MCP.",
    host="0.0.0.0", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
W = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
WO = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

def need(role):
    r = ROLE.get()
    if r != role and not (role == "operator" and r == "owner"):
        raise L.LaunchError(f"requires {role} token")
    return r
def safe(fn):
    def w(*a, **k):
        try: return fn(*a, **k)
        except L.LaunchError as e: return {"error": str(e)}
    import functools
    return functools.wraps(fn)(w)

@lm.tool(annotations=RO, description="List agent templates, their allowed scopes, latest method version and review rubric.")
@safe
def list_templates() -> dict:
    need("operator"); return {"templates": L.list_templates()}

@lm.tool(annotations=W, description="Create a draft agent instance from a template with minimal scopes, a metered budget cap, an approval mode (ask_first or auto_safe) and a pinned method version. Does not launch it.")
@safe
def create_agent_instance(template: str, name: str, budget_cap_cents: int = 50, approval_mode: str = "ask_first", method_version: int | None = None) -> dict:
    need("operator"); return L.create_instance(template, name, ROLE.get(), None, budget_cap_cents, approval_mode, method_version)

@lm.tool(annotations=W, description="Owner only. Launch a draft agent so it can accept jobs.")
@safe
def launch_agent(instance_id: str) -> dict:
    need("owner"); return L.launch_instance(instance_id, "owner")

@lm.tool(annotations=WO, description="Start an async job (poll with get_job). For ask_first agents the job waits in awaiting_approval until the owner approves, whatever channel started it. Fetches only public pages. Needs an idempotency_key.")
@safe
def start_job(instance_id: str, urls: list[str], idempotency_key: str) -> dict:
    need("operator"); return L.start_job(instance_id, urls, "mcp", idempotency_key)

@lm.tool(annotations=RO, description="Get job state, output, review verdict and flags.")
@safe
def get_job(job_id: str) -> dict:
    need("operator"); L.sweep(); return L.view_job(job_id) | {"output_detail": L.JOBS[job_id]["output"]}

@lm.tool(annotations=RO, description="Get the append-only, hash-chained event log of a job.")
@safe
def get_job_events(job_id: str) -> dict:
    need("operator"); return {"events": L.job_events(job_id)}

@lm.tool(annotations=RO, description="List jobs, optionally for one agent instance.")
@safe
def list_jobs(instance_id: str | None = None) -> dict:
    need("operator"); return {"jobs": L.list_jobs(instance_id)}

@lm.tool(annotations=WO, description="Owner only. Approve a job waiting in awaiting_approval; it then runs.")
@safe
def approve_job(job_id: str, note: str = "") -> dict:
    need("owner"); return L.approve_job(job_id, "owner", note)

@lm.tool(annotations=W, description="Owner only. Reject a job waiting for approval.")
@safe
def reject_job(job_id: str, note: str = "") -> dict:
    need("owner"); return L.reject_job(job_id, "owner", note)

@lm.tool(annotations=W, description="Owner only. Move an agent to a newer method version. Instances never upgrade silently.")
@safe
def upgrade_agent_method(instance_id: str, version: int) -> dict:
    need("owner"); return L.upgrade_instance(instance_id, version, "owner")

@lm.tool(annotations=RO, description="Job counts by status, metered cost, accepted jobs, cost per accepted job and silent-failure flags.")
@safe
def get_metrics() -> dict:
    need("operator"); return L.metrics()

async def info(request):
    rows = "".join(f"<li><b>{t['name']}</b> - {'runnable' if t['runnable'] else 'not runnable yet'}. {t['note']}</li>" for t in L.list_templates())
    rub = "".join(f"<li>{k}: {v}</li>" for k, v in L.RUBRIC.items())
    return HTMLResponse(f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Pillar launch layer</title>
<body style="font:16px/1.5 system-ui;max-width:720px;margin:2rem auto;padding:0 1rem"><h1>Pillar launch layer</h1>
<p>Template, instance, launch, job. Approvals, metered budgets, independent rubric review, versioned methods, MCP access. Private endpoint: <code>/launch/mcp</code> (bearer token required).</p>
<h2>Templates</h2><ul>{rows}</ul><h2>Review rubric</h2><ul>{rub}</ul>
<p>State is in memory and resets on restart. Costs are metered units of the checker, not payments.</p></body>""")
async def templates_json(request): return JSONResponse({"templates": L.list_templates()})
async def health(request): return PlainTextResponse("ok")
lm._custom_starlette_routes = []
lm.custom_route("/", methods=["GET"])(info)
lm.custom_route("/templates", methods=["GET"])(templates_json)
lm.custom_route("/healthz", methods=["GET"])(health)

def build(public_mcp):
    pub = public_mcp.streamable_http_app()
    prv = lm.streamable_http_app()
    @asynccontextmanager
    async def life(app):
        async with public_mcp.session_manager.run():
            async with lm.session_manager.run():
                yield
    app = Starlette(routes=[Mount("/launch", prv), Mount("/", pub)], lifespan=life)
    return Auth(app)
