"""Ingestion tools — create skills, add MCP servers, read and ingest from URLs.

These tools let agents create new capabilities from conversation.
Skills are YAML — safe, declarative, composable. No arbitrary code.
"""

import ipaddress
import json
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

from src.core.base import ToolContext, PROJECT_ROOT, TARS_TMP
from src.core.tools import tool, get_all_tools

logger = logging.getLogger(__name__)

_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


_PROJECT_ROOT = PROJECT_ROOT
_OVERLAY = os.environ.get("TARS_OVERLAY", "")

ALLOWED_PATH_ROOTS = (
    Path("/tmp"),
    TARS_TMP,
    _PROJECT_ROOT / "agents",
    _PROJECT_ROOT / "data",
    _PROJECT_ROOT / "codex",
    _PROJECT_ROOT / "skills",
    *(
        (Path(_OVERLAY),)
        if _OVERLAY
        else ()
    ),
)


def validate_file_path(file_path: str) -> str | None:
    """Validate a file path is within allowed directories. Returns error string or None if OK."""
    try:
        resolved = Path(file_path).resolve()
    except (ValueError, OSError):
        return f"Invalid file path: {file_path}"

    for root in ALLOWED_PATH_ROOTS:
        try:
            resolved.relative_to(root)
            return None
        except ValueError:
            continue

    return f"Access denied: {file_path} is outside allowed directories"


def _validate_url(url: str) -> tuple[str | None, str | None]:
    """Validate a URL is safe to fetch. Returns (error, resolved_ip).

    If error is not None, the URL is blocked.
    If error is None, resolved_ip contains the first safe IP to connect to
    (use this to prevent DNS rebinding — resolve once, pin the IP).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme}. Only http/https allowed.", None
    hostname = parsed.hostname
    if not hostname:
        return "No hostname in URL.", None
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}", None
    first_safe_ip = None
    for _, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        for net in _BLOCKED_NETS:
            if ip in net:
                return f"Blocked: {hostname} resolves to internal address {ip}.", None
        if first_safe_ip is None:
            first_safe_ip = str(ip)
    return None, first_safe_ip


@tool(
    name="create_skill",
    description="Create a new skill from conversation — becomes a slash command",
    category="system",
)
async def create_skill(
    ctx: ToolContext,
    name: str,
    description: str,
    prompt: str,
    tools: str = "memory_search",
) -> str:
    """Create a reusable skill that becomes available as a /slash command.

    Args:
        name: Skill name (lowercase, underscores ok, e.g. 'competitor_analysis')
        description: Short description shown in Discord
        prompt: The prompt template. Use {param_name} for parameters.
        tools: Comma-separated tool names the skill needs
    """
    from src.core.digest import ingest_skill_from_text

    # Validate name
    clean_name = name.lower().replace(" ", "_").replace("-", "_")
    if not clean_name.isidentifier():
        return f"Invalid skill name: {name}. Use lowercase letters, numbers, underscores."

    tool_list = [t.strip() for t in tools.split(",") if t.strip()]

    # Validate tools exist
    all_tools = get_all_tools()
    unknown = [t for t in tool_list if t not in all_tools]
    if unknown:
        return f"Unknown tools: {unknown}. Available: {list(all_tools.keys())}"

    # Extract parameters from {placeholders} in prompt
    import re
    params = [p for p in re.findall(r"\{(\w+)\}", prompt) if p.isidentifier()]
    parameters = {}
    if params:
        for p in set(params):
            parameters[p] = {
                "type": "string",
                "description": p.replace("_", " ").title(),
                "required": True,
            }

    path = ingest_skill_from_text(
        name=clean_name,
        description=description,
        prompt=prompt,
        tools=tool_list,
        parameters=parameters if parameters else None,
    )

    param_str = ", ".join(f"`{p}`" for p in parameters) if parameters else "none"
    return (
        f"Skill '{clean_name}' created at {path}\n"
        f"Parameters: {param_str}\n"
        f"Tools: {', '.join(tool_list)}\n"
        f"Available as `/{clean_name.replace('_', '-')}` after next command sync."
    )


@tool(
    name="read_url",
    description="Read content from a URL",
    category="research",
)
async def read_url(ctx: ToolContext, url: str, max_length: int = 10000) -> str:
    """Fetch and return the text content of a URL.

    Useful for ingesting skill definitions, documentation, or data.
    """
    err, _resolved_ip = _validate_url(url)
    if err:
        return err
    try:
        req = Request(url, headers={"User-Agent": "T.A.R.S/0.1"})
        with urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_length + 1000)

        text = raw.decode("utf-8", errors="replace")

        # Basic HTML stripping for web pages
        if "html" in content_type.lower():
            import re
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()

        if len(text) > max_length:
            text = text[:max_length] + "\n\n[truncated]"

        return text

    except URLError as e:
        return f"Failed to fetch URL: {e}"
    except Exception as e:
        return f"Error reading URL: {e}"


@tool(
    name="download_file",
    description="Download a file from a URL to a local temp path. Returns the file path for use with Claude's Read tool (images, PDFs) or other processing.",
    category="research",
)
async def download_file(ctx: ToolContext, url: str, filename: str | None = None) -> str:
    """Download a file (image, PDF, etc.) from a URL and save it locally.

    Returns the local file path. Use Claude's built-in Read tool to view
    downloaded images or PDFs.
    """
    import tempfile

    err, _resolved_ip = _validate_url(url)
    if err:
        return err

    max_size = 50 * 1024 * 1024  # 50 MB limit

    try:
        req = Request(url, headers={"User-Agent": "T.A.R.S/0.1"})
        with urlopen(req, timeout=30) as resp:
            data = resp.read(max_size + 1)
            if len(data) > max_size:
                return f"File too large (>{max_size // 1024 // 1024} MB)"

            # Determine filename
            if not filename:
                from urllib.parse import unquote
                path = urlparse(url).path
                filename = unquote(path.split("/")[-1]) or "download"
                # Strip query params that got into filename
                if "?" in filename:
                    filename = filename.split("?")[0]

            suffix = Path(filename).suffix or ""
            dl_dir = TARS_TMP / "scratch"
            dl_dir.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, prefix="tars-dl-",
                dir=str(dl_dir),
            )
            tmp.write(data)
            tmp.close()

            return f"Downloaded to: {tmp.name} ({len(data)} bytes)"

    except URLError as e:
        return f"Failed to download: {e}"
    except Exception as e:
        return f"Error downloading file: {e}"


@tool(
    name="browse_url",
    description="Browse a URL with a full browser — renders JavaScript, handles SPAs and dynamic content",
    category="research",
)
async def browse_url(
    ctx: ToolContext,
    url: str,
    wait_seconds: int = 3,
    max_length: int = 15000,
) -> str:
    """Fetch a URL using a headless browser with full JS rendering.

    Use this instead of read_url when:
    - The page uses JavaScript to load content (SPAs, React, etc.)
    - read_url returns empty/useless content
    - You need content that loads dynamically

    Args:
        url: The URL to browse
        wait_seconds: Seconds to wait for JS to render (1-10, default 3)
        max_length: Max characters to return (default 15000)
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "Error: playwright not installed. Run: uv add playwright && playwright install chromium"

    err, _resolved_ip = _validate_url(url)
    if err:
        return err

    wait_seconds = max(1, min(10, wait_seconds))

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(wait_seconds * 1000)

            # Extract readable text content
            text = await page.evaluate("""() => {
                // Remove script, style, nav, footer, header noise
                for (const el of document.querySelectorAll(
                    'script, style, noscript, nav, footer, header, iframe, svg'
                )) { el.remove(); }
                return document.body ? document.body.innerText : document.documentElement.innerText;
            }""")

            title = await page.title()
            await browser.close()

        text = text.strip()
        if not text:
            return f"Page loaded but no text content extracted from: {url}"

        if len(text) > max_length:
            text = text[:max_length] + "\n\n[truncated]"

        return f"**{title}**\n\n{text}" if title else text

    except Exception as e:
        return f"Browse error: {e}"


@tool(
    name="install_mcp",
    description="Connect a new MCP server — its tools become available to all agents automatically",
    category="system",
    hitl=True,
)
async def install_mcp(
    ctx: ToolContext,
    name: str,
    transport: str = "sse",
    url: str = "",
    command: str = "",
    args: str = "",
    cwd: str = "",
    headers: str = "",
    description: str = "",
) -> str:
    """Add an MCP server. Writes to mcp.yaml and regenerates .mcp.json for all agents.

    For remote servers (sse):
        install_mcp(name="kapoq", url="https://mcp.kapoq.com/mcp")
        install_mcp(name="kapoq", url="https://...", headers="Authorization: Bearer token123")

    For local servers (stdio):
        install_mcp(name="fs", transport="stdio", command="npx", args="@modelcontextprotocol/server-filesystem /home/docs")

    Args:
        name: Server name (e.g. 'kapoq', 'google-workspace')
        transport: 'sse' for remote, 'stdio' for local subprocess
        url: Server URL (required for sse)
        command: Executable path (required for stdio)
        args: Space-separated arguments for stdio command
        cwd: Working directory for stdio server
        headers: HTTP headers as 'Key: Value' lines (one per line, for sse auth)
        description: Human-readable description of the server
    """
    import json as _json
    from src.core.digest import ingest_mcp_server

    # Parse headers from "Key: Value" lines
    parsed_headers = {}
    if headers:
        for line in headers.strip().splitlines():
            if ": " in line:
                k, v = line.split(": ", 1)
                parsed_headers[k.strip()] = v.strip()

    # Parse args from space-separated string
    parsed_args = args.split() if args else None

    try:
        ingest_mcp_server(
            name,
            transport=transport,
            url=url,
            command=command,
            args=parsed_args,
            cwd=cwd,
            headers=parsed_headers or None,
            description=description,
        )
    except ValueError as e:
        return f"Error: {e}"

    # Verify by reading back
    from src.core.digest import _load_mcp_servers
    servers = _load_mcp_servers()
    server = servers.get(name, {})

    result = f"MCP server **{name}** installed ({transport}).\n"
    if url:
        result += f"URL: {url}\n"
    if command:
        result += f"Command: {command} {args}\n"
    if parsed_headers:
        result += f"Auth: {len(parsed_headers)} header(s) configured\n"

    result += "\n.mcp.json regenerated for all agents. Restart agents to connect."
    return result


@tool(
    name="list_capabilities",
    description="List all available tools, skills, and MCP servers",
    category="system",
)
async def list_capabilities(ctx: ToolContext) -> str:
    """Show everything the system can do — tools, skills, MCP servers."""
    from src.core.skills import get_all_skills

    lines = []

    # Tools
    tools = get_all_tools()
    lines.append(f"**Tools ({len(tools)}):**")
    by_category: dict[str, list] = {}
    for name, td in sorted(tools.items()):
        by_category.setdefault(td.category, []).append(f"  `{name}` — {td.description}")
    for cat in sorted(by_category):
        lines.append(f"  *{cat}:*")
        lines.extend(by_category[cat])

    # Skills
    skills = get_all_skills()
    if skills:
        lines.append(f"\n**Skills ({len(skills)}):**")
        for name, skill in sorted(skills.items()):
            params = ", ".join(p.name for p in skill.parameters) if skill.parameters else ""
            lines.append(f"  `/{name.replace('_', '-')}` ({params}) — {skill.description}")

    # MCP
    mcp_path = PROJECT_ROOT / "config" / "mcp.yaml"
    if mcp_path.exists():
        import yaml
        with open(mcp_path) as f:
            mcp = yaml.safe_load(f) or {}
        servers = mcp.get("servers", {})
        if servers:
            lines.append(f"\n**MCP Servers ({len(servers)}):**")
            for name, cfg in servers.items():
                lines.append(f"  `{name}` — {cfg.get('url', 'N/A')} ({cfg.get('transport', 'sse')})")

    return "\n".join(lines)
