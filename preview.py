#!/usr/bin/env python3
"""Run the whole site stack on your own machine, before pushing anything.

    python3 preview.py            build everything and serve it
    python3 preview.py --check    the same, then test every page and stop
    python3 preview.py --logs     follow the logs
    python3 preview.py --down     stop and clean up

Needs Docker running. Nothing else — no AWS, no credentials, no boto3.

It builds the same containers the server builds, puts the same Caddy in
front of them, and uses the same rules about which folders count. If it
works here it is the same thing that will be running there.

Everything it creates lives in .preview/ and is thrown away by --down.
Your folders are never written to.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".preview"
BUILD = WORK / "build"
PROJECT = "mfd-preview"

CADDY_PORT = 8080          # the proxy, routing by hostname
FIRST_DIRECT_PORT = 8001   # each site also gets its own plain port

FOLDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
SKIP_DIRS = {".git", ".github", "scripts", "__pycache__", ".preview"}

# Must stay identical to the one in deploy.py, or local and live differ.
DEFAULT_DOCKERFILE = """# Generated because this folder has no Dockerfile of its own.
FROM nginx:1.27-alpine
COPY . /usr/share/nginx/html
RUN rm -f /usr/share/nginx/html/site.json /usr/share/nginx/html/Dockerfile
EXPOSE 80
"""

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = OFF = ""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    log(f"\n{RED}ERROR{OFF}  {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Which folders are sites — the same three outcomes deploy.py uses
# ---------------------------------------------------------------------------

def load_sites() -> tuple[list[dict], list[tuple[str, str, str]]]:
    sites: list[dict] = []
    problems: list[tuple[str, str, str]] = []

    for manifest in sorted(ROOT.glob("*/site.json")):
        folder = manifest.parent
        name = folder.name
        if name.startswith(".") or name in SKIP_DIRS:
            continue
        if not FOLDER_RE.match(name):
            problems.append((name, "folder name can't be a container name", "broken"))
            continue
        try:
            data = json.loads(manifest.read_text())
        except json.JSONDecodeError as exc:
            problems.append((name, f"site.json is not valid JSON: {exc}", "broken"))
            continue
        if not isinstance(data, dict):
            problems.append((name, "site.json must contain a JSON object", "broken"))
            continue

        domain = str(data.get("domain", "")).strip().lower()
        if not domain:
            problems.append((name, "no domain set yet — ignored", "notready"))
            continue
        if not HOSTNAME_RE.match(domain):
            problems.append((name, f"'{domain}' is not a valid hostname", "broken"))
            continue

        try:
            port = int(data.get("port", 80))
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            problems.append((name, f"port {data.get('port')!r} is not a sensible number", "broken"))
            continue

        has_dockerfile = (folder / "Dockerfile").exists()
        if not has_dockerfile and not (folder / "index.html").exists():
            problems.append((name, "no index.html and no Dockerfile", "broken"))
            continue

        sites.append({
            "folder": name,
            "path": folder,
            "domain": domain,
            "port": port,
            "own_dockerfile": has_dockerfile,
        })

    return sites, problems


def pages_in(folder: Path) -> list[str]:
    """Every URL path this folder serves, found by looking for index.html.

    orsa-parcels/index.html           ->  /
    orsa-parcels/tracking/index.html  ->  /tracking/

    This is how the preview knows to test /tracking/ without being told.
    """
    paths = []
    for page in sorted(folder.rglob("index.html")):
        rel = page.parent.relative_to(folder)
        if any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts):
            continue
        paths.append("/" if rel == Path(".") else "/" + "/".join(rel.parts) + "/")
    return paths or ["/"]


# ---------------------------------------------------------------------------
# Build the stack
# ---------------------------------------------------------------------------

def stage(sites: list[dict]) -> None:
    """Copy each folder into .preview/build so a generated Dockerfile never
    has to be written into your actual repo."""
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    for site in sites:
        dest = BUILD / site["folder"]
        shutil.copytree(
            site["path"], dest,
            ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"),
        )
        if not site["own_dockerfile"]:
            (dest / "Dockerfile").write_text(DEFAULT_DOCKERFILE)


def write_caddyfile(sites: list[dict]) -> None:
    lines = [
        "# Generated by preview.py. Local only — plain HTTP, no certificates.",
        "{",
        f"\tauto_https off",
        "\tadmin off",
        "}",
        "",
    ]
    for site in sites:
        lines += [
            f"http://{site['folder']}.localhost:{CADDY_PORT} {{",
            f"\treverse_proxy {site['folder']}:{site['port']}",
            "}",
            "",
        ]
    # Same catch-all as production: an unknown hostname gets a flat 404
    # rather than whichever site happens to be first.
    lines += [f"http://:{CADDY_PORT} {{", "\trespond 404", "}", ""]
    (WORK / "Caddyfile").write_text("\n".join(lines))


def write_compose(sites: list[dict]) -> None:
    lines = ["# Generated by preview.py. Do not edit; it is rewritten each run.",
             "services:", ""]

    for i, site in enumerate(sites):
        name, port = site["folder"], site["port"]
        site["direct"] = FIRST_DIRECT_PORT + i
        lines += [
            f"  {name}:",
            "    build:",
            f"      context: ./build/{name}",
            f"    container_name: {PROJECT}-{name}",
            "    networks: [preview]",
            "    ports:",
            f'      - "127.0.0.1:{site["direct"]}:{port}"',
            "    healthcheck:",
            f'      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:{port}/ >/dev/null 2>&1 || exit 1"]',
            "      interval: 5s",
            "      timeout: 3s",
            "      retries: 12",
            "      start_period: 5s",
            "",
        ]

    # No depends_on, for the same reason as production: one broken folder
    # must not stop the proxy and take every other site down with it.
    lines += [
        "  caddy:",
        "    image: caddy:2-alpine",
        f"    container_name: {PROJECT}-caddy",
        "    networks: [preview]",
        "    ports:",
        f'      - "127.0.0.1:{CADDY_PORT}:{CADDY_PORT}"',
        "    volumes:",
        "      - ./Caddyfile:/etc/caddy/Caddyfile:ro",
        "",
        "networks:",
        "  preview: {}",
        "",
    ]
    (WORK / "docker-compose.yml").write_text("\n".join(lines))


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", "-f", str(WORK / "docker-compose.yml"), "-p", PROJECT, *args]
    return subprocess.run(cmd, check=check)


# ---------------------------------------------------------------------------
# Check every page actually answers
# ---------------------------------------------------------------------------

def get(url: str, host: str | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "")
    except Exception as e:                       # noqa: BLE001
        return 0, str(e)


def smoke(sites: list[dict]) -> bool:
    log(f"\n{DIM}Checking every page…{OFF}\n")
    ok = True

    for site in sites:
        for path in pages_in(site["path"]):
            # direct at the container
            code, _ = get(f"http://127.0.0.1:{site['direct']}{path}")
            # and through Caddy, which is what the server does
            via, _ = get(f"http://127.0.0.1:{CADDY_PORT}{path}", host=f"{site['folder']}.localhost")
            good = code == 200 and via == 200
            ok &= good
            mark = f"{GREEN}ok{OFF}" if good else f"{RED}FAILED{OFF}"
            log(f"  {mark:<16} {site['folder']}{path}"
                + ("" if good else f"   {DIM}direct={code} via-caddy={via}{OFF}"))

    # an unknown hostname must get a flat 404, not somebody's homepage
    code, _ = get(f"http://127.0.0.1:{CADDY_PORT}/", host="not-a-real-site.localhost")
    good = code == 404
    ok &= good
    log(f"  {(GREEN + 'ok' + OFF) if good else (RED + 'FAILED' + OFF):<16} "
        f"unknown hostname gets 404" + ("" if good else f"   {DIM}got {code}{OFF}"))
    return ok


def table(sites: list[dict]) -> None:
    log(f"\n{GREEN}Running.{OFF} Open any of these:\n")
    width = max(len(s["folder"]) for s in sites) + 2
    for site in sites:
        log(f"  {site['folder']:<{width}} {DIM}->{OFF} http://localhost:{site['direct']}")
        for path in pages_in(site["path"])[1:]:
            log(f"  {'':<{width}}    http://localhost:{site['direct']}{path}")
    log(f"\n  {DIM}Through Caddy, exactly as the server routes it:{OFF}")
    for site in sites:
        log(f"  {site['folder']:<{width}} {DIM}->{OFF} http://{site['folder']}.localhost:{CADDY_PORT}")
    log(f"\n  {DIM}.localhost names work in Chrome, Firefox and Safari with no setup."
        f"\n  From a terminal:  curl -H 'Host: {sites[0]['folder']}.localhost' "
        f"http://127.0.0.1:{CADDY_PORT}/{OFF}")
    log(f"\n  {DIM}python3 preview.py --logs    follow the logs{OFF}")
    log(f"  {DIM}python3 preview.py --down    stop everything{OFF}\n")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Run the sites locally in Docker.")
    ap.add_argument("--check", action="store_true", help="test every page, then stop")
    ap.add_argument("--logs", action="store_true", help="follow the container logs")
    ap.add_argument("--down", action="store_true", help="stop and remove everything")
    args = ap.parse_args()

    if shutil.which("docker") is None:
        die("Docker isn't installed, or isn't on PATH.")

    if args.down:
        if (WORK / "docker-compose.yml").exists():
            compose("down", "--remove-orphans", "-v", check=False)
        shutil.rmtree(WORK, ignore_errors=True)
        log(f"{GREEN}Stopped.{OFF} .preview/ removed.")
        return 0

    if args.logs:
        if not (WORK / "docker-compose.yml").exists():
            die("Nothing is running. Start it with: python3 preview.py")
        compose("logs", "-f", check=False)
        return 0

    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        die("Docker is installed but not running. Start Docker Desktop and try again.")

    sites, problems = load_sites()

    for folder, reason, kind in problems:
        colour = YELLOW if kind == "notready" else RED
        label = "draft" if kind == "notready" else "broken"
        log(f"  {colour}{label:<8}{OFF} {folder}  {DIM}{reason}{OFF}")
    if problems:
        log("")

    if not sites:
        die("No folders with a domain in site.json. Nothing to preview.")

    log(f"{DIM}Building {len(sites)} site(s)…{OFF}\n")
    WORK.mkdir(exist_ok=True)
    (WORK / ".gitignore").write_text("*\n")   # keeps the whole folder out of git
    stage(sites)
    write_compose(sites)
    write_caddyfile(sites)

    r = compose("up", "-d", "--build", "--remove-orphans", "--wait", "--wait-timeout", "180",
                check=False)
    if r.returncode != 0:
        log(f"\n{RED}A container did not come up.{OFF} "
            f"{DIM}See what it said:  python3 preview.py --logs{OFF}")
        return 1

    passed = smoke(sites)

    if args.check:
        compose("down", "--remove-orphans", check=False)
        log(f"\n{GREEN if passed else RED}{'All pages answered.' if passed else 'Something failed.'}{OFF}"
            f" {DIM}Stopped.{OFF}\n")
        return 0 if passed else 1

    table(sites)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())