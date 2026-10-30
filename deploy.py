#!/usr/bin/env python3
"""
multi-folder-domain — root provisioning and deployment script.

One persistent EC2 instance. Every folder runs in its own Docker container.
Caddy sits in front, terminates TLS, and routes each domain to its container.

Design notes
------------
STATE     AWS itself is the state store. The server is found by its tag
          (Name=multi-folder-domain-server). No state file, nothing to commit
          back, nothing to drift. Every run is idempotent.

ACCESS    No inbound SSH is required. Files and commands reach the instance
          through AWS Systems Manager, which is an outbound connection from
          the instance. Port 22 only opens if ADMIN_CIDR is set, and then only
          to that one address.

CONTAINERS  The script generates a docker-compose file from the folder
          manifests and runs it with --remove-orphans. That one flag is what
          makes deleting a folder actually delete its container.

CADDY     The Caddyfile is REGENERATED in full every run, never appended to.
          Appending drifts; regenerating cannot.

ROLLBACK  The previous release stays on disk as /srv/mfd.prev. If a deploy
          fails its health check, the script puts the old one back by itself.

Usage
-----
  python deploy.py                 provision + deploy
  python deploy.py --plan          show what would change, change nothing
  python deploy.py --status        what is running right now
  python deploy.py --logs FOLDER   last 100 log lines from one container
  python deploy.py --rollback      put the previous release back
  python deploy.py --destroy       terminate the instance, release the IP
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, NoReturn

import boto3
from botocore.exceptions import ClientError

# ----------------------------------------------------------------------------
# Configuration — every default can be overridden by an environment variable.
# ----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent


def _load_config() -> dict[str, Any]:
    """config.json sits next to this script and is committed with the code.

    It replaces GitHub repository variables entirely: edit it, commit it, done.
    An environment variable of the same name in CAPITALS still wins, so a
    one-off local run can override anything without touching the file.
    """
    path = ROOT / "config.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"\nERROR: config.json is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if not isinstance(data, dict):
        print("\nERROR: config.json must contain a JSON object.", file=sys.stderr)
        raise SystemExit(1)
    return data


CONFIG = _load_config()
TRUTHY = {"1", "true", "yes", "on"}


def setting(key: str, default: Any = "") -> Any:
    """Environment variable, then config.json, then the built-in default."""
    env = os.environ.get(key.upper())
    if env is not None and env.strip() != "":
        return env.strip()
    value = CONFIG.get(key, default)
    return value


def flag(key: str, default: bool = False) -> bool:
    value = setting(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUTHY


def number(key: str, default: int) -> int:
    try:
        return int(setting(key, default))
    except (TypeError, ValueError):
        print(
            f"\nERROR: '{key}' in config.json must be a whole number, got "
            f"{setting(key, default)!r}.",
            file=sys.stderr,
        )
        raise SystemExit(1)


REGION = str(setting("region", os.environ.get("AWS_REGION", "us-east-1")))
PROJECT = str(setting("project_name", "multi-folder-domain"))
SERVER_TAG = str(setting("server_tag", f"{PROJECT}-server"))
INSTANCE_TYPE = str(setting("instance_type", "t3.micro"))
DISK_GB = number("disk_gb", 20)          # Docker images need the room.
ADMIN_CIDR = str(setting("admin_cidr", "")).strip()
ACME_EMAIL = str(setting("acme_email", "")).strip()
SSH_KEY_NAME = str(setting("ssh_key_name", "")).strip()

# Let's Encrypt staging. Certificates are untrusted by browsers but the rate
# limits are enormous. Leave this ON until the whole pipeline works, or one bad
# afternoon of retries can lock the real certificates out for a week.
ACME_STAGING = flag("acme_staging", True)

# Route 53. Off by default, because it writes to DNS.
MANAGE_DNS = flag("manage_dns", False)

# A folder with a real mistake in it (typo'd hostname, broken JSON, two folders
# claiming one domain) marks the run as failed so it doesn't go unnoticed. A
# folder that simply has no domain yet never does. Set this to false to make
# even the real mistakes warnings only.
FAIL_ON_SKIPPED = flag("fail_on_skipped", True)

# The off switch. When true, every run stops the instance instead of deploying
# — and does nothing at all if it is already stopped. Stopping is not deleting:
# the disk, the Elastic IP and everything on them survive, and a start brings
# the sites back on their own.
SERVER_OFF = flag("server_off", False)

SG_NAME = f"{PROJECT}-sg"
ROLE_NAME = f"{PROJECT}-ec2-role"
PROFILE_NAME = f"{PROJECT}-ec2-profile"
APP_DIR = "/srv/mfd"
PREV_DIR = "/srv/mfd.prev"
MAX_PAYLOAD_BYTES = 60_000

UBUNTU_SSM_PARAM = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/"
    "amd64/hvm/ebs-gp3/ami-id"
)

HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$"
)
FOLDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")
SKIP_DIRS = {"scripts", "__pycache__", "node_modules", ".git", ".github"}

ec2 = boto3.client("ec2", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
route53 = boto3.client("route53")


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def fail(msg: str) -> NoReturn:
    print(f"\nERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ----------------------------------------------------------------------------
# 1. Discover the folders
# ----------------------------------------------------------------------------

DEFAULT_DOCKERFILE = """# Generated by deploy.py because this folder has no Dockerfile of its own.
# Write your own Dockerfile here and it will be used instead, untouched.
FROM nginx:1.27-alpine
COPY . /usr/share/nginx/html
RUN rm -f /usr/share/nginx/html/site.json /usr/share/nginx/html/Dockerfile
EXPOSE 80
"""


def load_sites() -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Any directory holding a site.json is a site. That is the whole rule.

    A folder that isn't ready — no domain yet, a typo, no index.html — is
    SKIPPED, not fatal. One half-written folder must never stop the other
    sites from deploying; that is the same isolation Caddy gets by not
    depending on every container being healthy.

    A folder with NO site.json isn't a site at all — it is invisible here and
    never mentioned. Notes, drafts, scratch directories: all ignored.

    A folder that HAS a site.json is sorted into one of three outcomes:

      deployed  — everything it needs is there
      notready  — no domain set yet. A draft. Mentioned once, quietly,
                  and the run still passes.
      broken    — a typo, bad JSON, a clash. Something you meant to work
                  and doesn't. Listed loudly, and the run goes red unless
                  config.json sets "fail_on_skipped": false.

    Returns (good sites, problems) where each problem is
    (folder, reason, "notready" | "broken").
    """
    sites: list[dict[str, Any]] = []
    problems: list[tuple[str, str, str]] = []

    for manifest in sorted(ROOT.glob("*/site.json")):
        folder = manifest.parent
        name = folder.name
        if name.startswith(".") or name in SKIP_DIRS:
            continue

        if not FOLDER_RE.match(name):
            problems.append((name, "folder name can't be a container name — use "
                                   "lowercase letters, numbers, dashes, underscores", "broken"))
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

        raw_aliases = data.get("aliases", [])
        if not isinstance(raw_aliases, list):
            problems.append((name, "'aliases' must be a list", "broken"))
            continue
        aliases = [str(a).strip().lower() for a in raw_aliases if str(a).strip()]
        bad_alias = next((a for a in aliases if not HOSTNAME_RE.match(a)), None)
        if bad_alias:
            problems.append((name, f"alias '{bad_alias}' is not a valid hostname", "broken"))
            continue

        try:
            port = int(data.get("port", 80))
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            problems.append((name, f"port {data.get('port')!r} is not a number between 1 and 65535", "broken"))
            continue

        has_dockerfile = (folder / "Dockerfile").exists()
        if not has_dockerfile and not (folder / "index.html").exists():
            problems.append((name, "no index.html and no Dockerfile — needs one or the other", "broken"))
            continue

        sites.append(
            {
                "folder": name,
                "path": folder,
                "domain": domain,
                "aliases": aliases,
                "port": port,
                "own_dockerfile": has_dockerfile,
            }
        )

    # A domain claimed twice is genuinely ambiguous — there is no way to know
    # which folder should win, and guessing would silently serve the wrong
    # site. Both are dropped, so that domain goes down loudly rather than
    # serving something unpredictable. The other folders still deploy.
    claimed: dict[str, list[str]] = {}
    for site in sites:
        for host in [site["domain"], *site["aliases"]]:
            claimed.setdefault(host, []).append(site["folder"])

    conflicted = {f for host, folders in claimed.items() if len(folders) > 1 for f in folders}
    for host, folders in sorted(claimed.items()):
        if len(folders) > 1:
            for f in sorted(set(folders)):
                problems.append((f, f"'{host}' is claimed by {' and '.join(sorted(set(folders)))} "
                                    "— all of them skipped, since there is no way to pick one", "broken"))
    sites = [s for s in sites if s["folder"] not in conflicted]

    if not sites:
        for folder, why, _ in problems:
            log(f"{folder}: {why}")
        fail(
            "No deployable folders. Every folder with a site.json has a problem, "
            "or there are none at all — so there is nothing to deploy and the "
            "server was left exactly as it was."
        )

    return sites, problems


# ----------------------------------------------------------------------------
# 2. Generated config
# ----------------------------------------------------------------------------

def build_caddyfile(sites: list[dict[str, Any]]) -> str:
    globals_: list[str] = []
    if ACME_EMAIL:
        globals_.append(f"\temail {ACME_EMAIL}")
    if ACME_STAGING:
        globals_.append("\tacme_ca https://acme-staging-v02.api.letsencrypt.org/directory")

    # An empty global block is a parse error in Caddy, so only emit it when
    # there is something to put inside it.
    lines: list[str] = ["{", *globals_, "}", ""] if globals_ else []

    lines += [
        "# GENERATED BY deploy.py — do not edit on the server.",
        "# Every deploy rewrites this file from the folders' site.json files.",
        "",
    ]

    for site in sites:
        hosts = ", ".join([site["domain"], *site["aliases"]])
        lines += [
            f"{hosts} {{",
            f"\treverse_proxy {site['folder']}:{site['port']}",
            "\tencode zstd gzip",
            "\theader {",
            '\t\tStrict-Transport-Security "max-age=31536000; includeSubDomains"',
            "\t\tX-Content-Type-Options nosniff",
            "\t\tReferrer-Policy strict-origin-when-cross-origin",
            "\t}",
            "}",
            "",
        ]

    # A hostname pointed here but not configured gets a flat 404, rather than
    # being quietly served whichever site happens to be first.
    lines += [":80, :443 {", "\trespond 404", "}", ""]
    return "\n".join(lines)


def build_compose(sites: list[dict[str, Any]], caddyfile: str = "") -> str:
    # The Caddyfile reaches the container as a bind mount, and Compose decides
    # whether to recreate a container by comparing SERVICE DEFINITIONS, not the
    # contents of mounted files. So a Caddyfile-only change is invisible to it
    # and the old config keeps running — silently, with everything reporting
    # healthy.
    #
    # Stamping the file's hash into the service definition makes the change
    # visible to Compose, which then recreates the container by itself. This
    # does not depend on the admin API, on `caddy reload` succeeding, or on
    # anything else working.
    caddy_sha = hashlib.sha256(caddyfile.encode()).hexdigest()[:16]

    lines = [
        "# GENERATED BY deploy.py — do not edit on the server.",
        "name: mfd",
        "",
        "networks:",
        "  mfd-net:",
        "    driver: bridge",
        "",
        "volumes:",
        "  caddy_data:",
        "  caddy_config:",
        "",
        "services:",
        "  caddy:",
        "    image: caddy:2-alpine",
        "    container_name: mfd-caddy",
        "    restart: unless-stopped",
        "    ports:",
        '      - "80:80"',
        '      - "443:443"',
        '      - "443:443/udp"',
        "    volumes:",
        "      - ./Caddyfile:/etc/caddy/Caddyfile:ro",
        "      - caddy_data:/data",
        "      - caddy_config:/config",
        "    environment:",
        f"      CADDYFILE_SHA: \"{caddy_sha}\"",
        "    networks: [mfd-net]",
        # Deliberately NO depends_on. If Caddy waited for every folder to be
        # healthy, one bad Dockerfile would stop the proxy starting and take
        # every other site down with it. Caddy starts regardless; a broken
        # backend returns 502 for its own domain and nothing else.
        "",
    ]

    for site in sites:
        name = site["folder"]
        port = site["port"]
        lines += [
            f"  {name}:",
            "    build:",
            f"      context: ./{name}",
            f"    image: mfd/{name}:latest",
            f"    container_name: mfd-{name}",
            "    restart: unless-stopped",
            "    networks: [mfd-net]",
            "    expose:",
            f'      - "{port}"',
            "    healthcheck:",
            f'      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:{port}/ >/dev/null 2>&1 || exit 1"]',
            "      interval: 20s",
            "      timeout: 5s",
            "      retries: 5",
            "      start_period: 20s",
            "    logging:",
            "      driver: json-file",
            "      options:",
            '        max-size: "10m"',
            '        max-file: "3"',
            "",
        ]

    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 3. Networking
# ----------------------------------------------------------------------------

def default_vpc_id() -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        fail("No default VPC in this region. Create one, or set a VPC id in the script.")
    return vpcs[0]["VpcId"]


def ensure_security_group(plan: bool) -> str:
    step("Security group")
    vpc_id = default_vpc_id()
    found = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [SG_NAME]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]

    if found:
        sg_id = found[0]["GroupId"]
        log(f"reusing {SG_NAME} ({sg_id})")
    elif plan:
        log(f"would create {SG_NAME}")
        return "sg-PLANNED"
    else:
        sg_id = ec2.create_security_group(
            GroupName=SG_NAME,
            Description=f"{PROJECT}: web traffic in, SSM out",
            VpcId=vpc_id,
        )["GroupId"]
        ec2.create_tags(Resources=[sg_id], Tags=[{"Key": "Project", "Value": PROJECT}])
        log(f"created {SG_NAME} ({sg_id})")

    wanted = [
        ("tcp", 80, 80, "0.0.0.0/0", "HTTP, redirected to HTTPS by Caddy"),
        ("tcp", 443, 443, "0.0.0.0/0", "HTTPS"),
        ("udp", 443, 443, "0.0.0.0/0", "HTTP/3"),
    ]
    if ADMIN_CIDR:
        if not ADMIN_CIDR.endswith("/32"):
            fail(
                f"ADMIN_CIDR is '{ADMIN_CIDR}'. SSH opens to one address only, so "
                "this must end in /32."
            )
        wanted.append(("tcp", 22, 22, ADMIN_CIDR, "SSH, admin only"))
    else:
        log("no ADMIN_CIDR set — port 22 stays shut. Admin access is via SSM.")

    for proto, lo, hi, cidr, desc in wanted:
        if plan:
            log(f"would ensure {proto}/{lo} from {cidr}")
            continue
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": proto,
                        "FromPort": lo,
                        "ToPort": hi,
                        "IpRanges": [{"CidrIp": cidr, "Description": desc}],
                    }
                ],
            )
            log(f"opened {proto}/{lo} from {cidr}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvalidPermission.Duplicate":
                log(f"{proto}/{lo} from {cidr} already open")
            else:
                raise

    return sg_id


# ----------------------------------------------------------------------------
# 4. IAM
# ----------------------------------------------------------------------------

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


def ensure_instance_profile(plan: bool) -> str:
    step("IAM role and instance profile")
    try:
        iam.get_role(RoleName=ROLE_NAME)
        log(f"reusing role {ROLE_NAME}")
    except iam.exceptions.NoSuchEntityException:
        if plan:
            log(f"would create role {ROLE_NAME}")
            return PROFILE_NAME
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description=f"{PROJECT}: lets the instance be managed by SSM",
            Tags=[{"Key": "Project", "Value": PROJECT}],
        )
        log(f"created role {ROLE_NAME}")

    if not plan:
        iam.attach_role_policy(
            RoleName=ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        )

    try:
        iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
        log(f"reusing instance profile {PROFILE_NAME}")
    except iam.exceptions.NoSuchEntityException:
        if plan:
            log(f"would create instance profile {PROFILE_NAME}")
            return PROFILE_NAME
        iam.create_instance_profile(InstanceProfileName=PROFILE_NAME)
        log(f"created instance profile {PROFILE_NAME}")
        time.sleep(10)  # IAM is eventually consistent; EC2 rejects it if we rush.

    if not plan:
        profile = iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
        if ROLE_NAME not in [r["RoleName"] for r in profile["InstanceProfile"]["Roles"]]:
            iam.add_role_to_instance_profile(
                InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME
            )
            log("attached role to instance profile")
            time.sleep(10)

    return PROFILE_NAME


# ----------------------------------------------------------------------------
# 5. The instance
# ----------------------------------------------------------------------------

USER_DATA = f"""#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

snap install amazon-ssm-agent --classic || true
snap start amazon-ssm-agent || true

apt-get update -y
apt-get install -y ca-certificates curl gnupg

# Docker, from the official repository.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker
mkdir -p {APP_DIR}

# A small swap file. A t3.micro has 1 GB of RAM and image builds will
# otherwise be killed by the kernel partway through.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Keep old images from filling the disk.
cat > /etc/cron.weekly/docker-prune <<'CRON'
#!/bin/sh
docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
docker builder prune -af --filter "until=168h" >/dev/null 2>&1 || true
CRON
chmod +x /etc/cron.weekly/docker-prune

touch /var/lib/cloud/instance/{PROJECT}-bootstrapped
"""


def find_instance() -> dict[str, Any] | None:
    res = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [SERVER_TAG]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
    for reservation in res["Reservations"]:
        for inst in reservation["Instances"]:
            return inst
    return None


def require_instance() -> dict[str, Any]:
    inst = find_instance()
    if not inst:
        fail(f"No instance tagged Name={SERVER_TAG}. Run a deploy first.")
    return inst


def latest_ubuntu_ami() -> str:
    try:
        return ssm.get_parameter(Name=UBUNTU_SSM_PARAM)["Parameter"]["Value"]
    except ClientError as exc:
        fail(f"Could not read the Ubuntu AMI id from SSM public parameters: {exc}")


def ensure_instance(sg_id: str, profile_name: str, plan: bool) -> dict[str, Any]:
    step("EC2 instance")
    inst = find_instance()

    if inst and inst["State"]["Name"] == "stopped":
        if plan:
            log(f"would start stopped instance {inst['InstanceId']}")
            return inst
        log(f"{inst['InstanceId']} is stopped — starting it")
        ec2.start_instances(InstanceIds=[inst["InstanceId"]])
        ec2.get_waiter("instance_running").wait(InstanceIds=[inst["InstanceId"]])
        inst = find_instance()

    if inst:
        log(f"reusing {inst['InstanceId']} ({inst['State']['Name']}) — not creating another")
        return inst

    if plan:
        log(f"would launch a {INSTANCE_TYPE} tagged Name={SERVER_TAG}")
        return {"InstanceId": "i-PLANNED"}

    ami = latest_ubuntu_ami()
    log(f"launching {INSTANCE_TYPE} from {ami}")

    kwargs: dict[str, Any] = {
        "ImageId": ami,
        "InstanceType": INSTANCE_TYPE,
        "MinCount": 1,
        "MaxCount": 1,
        "SecurityGroupIds": [sg_id],
        "IamInstanceProfile": {"Name": profile_name},
        "UserData": USER_DATA,
        "MetadataOptions": {"HttpTokens": "required"},  # IMDSv2 only
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {
                    "VolumeSize": DISK_GB,
                    "VolumeType": "gp3",
                    "DeleteOnTermination": True,
                    "Encrypted": True,
                },
            }
        ],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": SERVER_TAG},
                    {"Key": "Project", "Value": PROJECT},
                ],
            }
        ],
    }
    if SSH_KEY_NAME:
        kwargs["KeyName"] = SSH_KEY_NAME

    instance_id = ec2.run_instances(**kwargs)["Instances"][0]["InstanceId"]
    log(f"waiting for {instance_id} to reach running")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    return find_instance()


# ----------------------------------------------------------------------------
# 6. Elastic IP
# ----------------------------------------------------------------------------

def ensure_elastic_ip(instance_id: str, plan: bool) -> str:
    step("Elastic IP")
    addrs = ec2.describe_addresses(
        Filters=[{"Name": "tag:Project", "Values": [PROJECT]}]
    )["Addresses"]

    if addrs:
        addr = addrs[0]
        if addr.get("InstanceId") == instance_id:
            log(f"{addr['PublicIp']} already attached")
            return addr["PublicIp"]
        if plan:
            log(f"would attach existing {addr['PublicIp']}")
            return addr["PublicIp"]
        ec2.associate_address(AllocationId=addr["AllocationId"], InstanceId=instance_id)
        log(f"attached {addr['PublicIp']}")
        return addr["PublicIp"]

    if plan:
        log("would allocate a new Elastic IP")
        return "0.0.0.0"

    alloc = ec2.allocate_address(
        Domain="vpc",
        TagSpecifications=[
            {
                "ResourceType": "elastic-ip",
                "Tags": [
                    {"Key": "Project", "Value": PROJECT},
                    {"Key": "Name", "Value": f"{PROJECT}-eip"},
                ],
            }
        ],
    )
    ec2.associate_address(AllocationId=alloc["AllocationId"], InstanceId=instance_id)
    log(f"allocated and attached {alloc['PublicIp']}")
    return alloc["PublicIp"]


# ----------------------------------------------------------------------------
# 7. DNS (optional — only runs when MANAGE_DNS is on)
# ----------------------------------------------------------------------------

def hosted_zones() -> list[dict[str, str]]:
    zones: list[dict[str, str]] = []
    paginator = route53.get_paginator("list_hosted_zones")
    for page in paginator.paginate():
        for zone in page["HostedZones"]:
            if zone.get("Config", {}).get("PrivateZone"):
                continue
            zones.append({"id": zone["Id"].split("/")[-1], "name": zone["Name"].rstrip(".")})
    return zones


def zone_for(domain: str, zones: list[dict[str, str]]) -> dict[str, str] | None:
    """Longest matching suffix wins, so sub.example.com prefers a sub zone."""
    matches = [z for z in zones if domain == z["name"] or domain.endswith("." + z["name"])]
    return max(matches, key=lambda z: len(z["name"])) if matches else None


def sync_dns(sites: list[dict[str, Any]], public_ip: str, plan: bool) -> list[str]:
    """Returns the list of names it could NOT handle, so they can be printed."""
    step("DNS (Route 53)")
    unmanaged: list[str] = []

    try:
        zones = hosted_zones()
    except ClientError as exc:
        log(f"could not list hosted zones ({exc.response['Error']['Code']}) — skipping")
        return [n for s in sites for n in [s["domain"], *s["aliases"]]]

    if not zones:
        log("no public hosted zones in this account")
        return [n for s in sites for n in [s["domain"], *s["aliases"]]]

    for site in sites:
        for name in [site["domain"], *site["aliases"]]:
            zone = zone_for(name, zones)
            if not zone:
                unmanaged.append(name)
                continue
            if plan:
                log(f"would set {name} A {public_ip} in zone {zone['name']}")
                continue
            route53.change_resource_record_sets(
                HostedZoneId=zone["id"],
                ChangeBatch={
                    "Comment": f"{PROJECT} deploy",
                    "Changes": [
                        {
                            "Action": "UPSERT",
                            "ResourceRecordSet": {
                                "Name": name,
                                "Type": "A",
                                "TTL": 300,
                                "ResourceRecords": [{"Value": public_ip}],
                            },
                        }
                    ],
                },
            )
            log(f"{name} -> {public_ip} (zone {zone['name']})")

    if unmanaged:
        log(f"{len(unmanaged)} name(s) are not in Route 53 — you'll set those by hand")
    return unmanaged


# ----------------------------------------------------------------------------
# 8. Shipping it — SSM, no SSH
# ----------------------------------------------------------------------------

def build_payload(sites: list[dict[str, Any]], caddyfile: str, compose: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def add_text(name: str, text: str) -> None:
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))

        for site in sites:
            for path in sorted(site["path"].rglob("*")):
                if path.is_dir():
                    continue
                rel = path.relative_to(ROOT)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                if any(part in SKIP_DIRS for part in rel.parts):
                    continue
                tar.add(path, arcname=str(rel))
            if not site["own_dockerfile"]:
                add_text(f"{site['folder']}/Dockerfile", DEFAULT_DOCKERFILE)

        add_text("Caddyfile", caddyfile)
        add_text("docker-compose.yml", compose)

    return buf.getvalue()


def wait_for_ssm(instance_id: str, timeout: int = 600) -> None:
    step("Waiting for the instance to register with SSM")
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )["InstanceInformationList"]
        if info and info[0]["PingStatus"] == "Online":
            log("online")
            return
        time.sleep(10)
    fail(
        f"{instance_id} never came online in SSM. A brand new instance takes a few "
        "minutes to install Docker — re-run the workflow. If it keeps failing, check "
        "that the instance profile is attached and the subnet reaches the internet."
    )


class RemoteError(RuntimeError):
    """A command failed on the instance. Raised so the caller can roll back."""


def wait_for_docker(instance_id: str, timeout: int = 900) -> None:
    """The SSM agent reports Online well before cloud-init has finished, so a
    first deploy can land while apt is still installing Docker. Wait for the
    bootstrap marker rather than trusting SSM's own readiness."""
    step("Waiting for the server bootstrap to finish")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            out = run_remote(
                instance_id,
                [
                    "command -v docker >/dev/null 2>&1 || { echo NOT_READY; exit 0; }",
                    "docker compose version >/dev/null 2>&1 || { echo NOT_READY; exit 0; }",
                    "systemctl is-active --quiet docker || { echo NOT_READY; exit 0; }",
                    "echo READY",
                ],
                "check bootstrap",
                timeout=120,
                quiet=True,
                raise_on_error=True,
            )
        except RemoteError:
            out = "NOT_READY"
        if "READY" in out and "NOT_READY" not in out:
            log("Docker is installed and running")
            return
        if attempt == 1:
            log("still installing Docker — this takes a few minutes on a new server")
        time.sleep(20)
    fail(
        "The server never finished installing Docker. Check the instance's "
        "/var/log/cloud-init-output.log via Session Manager."
    )


def run_remote(
    instance_id: str,
    commands: list[str],
    label: str,
    timeout: int = 1800,
    quiet: bool = False,
    raise_on_error: bool = False,
) -> str:
    cmd_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment=label[:100],
        Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
        TimeoutSeconds=600,
    )["Command"]["CommandId"]

    deadline = time.time() + timeout + 120
    while time.time() < deadline:
        time.sleep(4)
        try:
            inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvocationDoesNotExist":
                continue
            raise
        if inv["Status"] in {"Pending", "InProgress", "Delayed"}:
            continue
        out = inv.get("StandardOutputContent", "")
        if inv["Status"] == "Success":
            if out.strip() and not quiet:
                for line in out.strip().splitlines()[-25:]:
                    log(line)
            return out
        detail = (
            f"{label} failed on the instance ({inv['Status']}).\n\n"
            f"--- stdout ---\n{out[-2000:]}\n"
            f"--- stderr ---\n{inv.get('StandardErrorContent', '')[-3000:]}"
        )
        if raise_on_error:
            raise RemoteError(detail)
        fail(detail)
    msg = f"{label} timed out after {timeout}s."
    if raise_on_error:
        raise RemoteError(msg)
    fail(msg)


def deploy_payload(instance_id: str, payload: bytes) -> None:
    step("Shipping the folders and config")
    encoded = base64.b64encode(payload).decode()
    log(f"payload {len(payload):,} bytes raw, {len(encoded):,} encoded")

    if len(encoded) > MAX_PAYLOAD_BYTES:
        fail(
            f"Payload is {len(encoded):,} bytes encoded, past the {MAX_PAYLOAD_BYTES:,} "
            "limit for an inline SSM command. See the README section 'Growing past the "
            "inline limit' — the fix is to push the tarball to S3 and pull it instead."
        )

    run_remote(
        instance_id,
        [
            # SSM runs these with /bin/sh, which is dash on Ubuntu. dash has
            # no "pipefail", so keep every command POSIX.
            "set -eu",
            "umask 022",
            "rm -rf /tmp/mfd && mkdir -p /tmp/mfd",
            f"echo '{encoded}' | base64 -d > /tmp/mfd/payload.tgz",
            "tar xzf /tmp/mfd/payload.tgz -C /tmp/mfd",
            "rm -f /tmp/mfd/payload.tgz",
            # Keep the release that is currently live, so a failure can go back.
            f"rm -rf {PREV_DIR}",
            f"if [ -d {APP_DIR} ] && [ -n \"$(ls -A {APP_DIR} 2>/dev/null)\" ]; then cp -a {APP_DIR} {PREV_DIR}; fi",
            f"rm -rf {APP_DIR} && mkdir -p {APP_DIR}",
            f"cp -a /tmp/mfd/. {APP_DIR}/",
            "rm -rf /tmp/mfd",
            f"cd {APP_DIR}",
            "docker compose config -q",
            # compose config checks the compose file, not the Caddyfile. A bad
            # Caddyfile would crash-loop the proxy, so check it in a throwaway
            # container first, using an image that is already on the host.
            "docker run --rm -v \"$PWD/Caddyfile\":/etc/caddy/Caddyfile:ro "
            "caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile",
            "echo '--- building and starting ---'",
            "docker compose up -d --build --remove-orphans --wait --wait-timeout 600",
            # The Caddyfile is a bind mount. Compose compares service
            # definitions, not file contents, so a changed Caddyfile alone
            # leaves the container running with the OLD config and nothing
            # looks wrong. Tell Caddy explicitly. The reload is graceful —
            # no restart, no dropped connections — and harmless if the
            # container was just recreated anyway.
            "echo '--- reloading caddy ---'",
            "docker compose exec -T caddy caddy reload "
            "--config /etc/caddy/Caddyfile --adapter caddyfile "
            "|| docker compose restart caddy",
            "docker compose ps --format '{{.Name}}\\t{{.State}}\\t{{.Status}}'",
        ],
        "deploy containers",
        raise_on_error=True,
    )


def health_check(instance_id: str, sites: list[dict[str, Any]]) -> bool:
    """Checks from inside the server, so it works before DNS or TLS exist."""
    step("Health check")
    checks = [
        f"docker compose exec -T caddy wget -q -T 10 -O /dev/null "
        f"http://{s['folder']}:{s['port']}/ && echo '{s['folder']} OK' "
        f"|| echo '{s['folder']} FAILED'"
        for s in sites
    ]
    out = run_remote(
        instance_id,
        ["set -u", f"cd {APP_DIR}", *checks],   # POSIX sh: no pipefail
        "health check",
        timeout=300,
        quiet=True,
    )

    seen, ok = set(), True
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1] not in {"OK", "FAILED"}:
            continue
        folder, verdict = parts
        seen.add(folder)
        log(f"{folder:<24} {'ok' if verdict == 'OK' else 'NOT RESPONDING'}")
        ok = ok and verdict == "OK"
    missing = {s["folder"] for s in sites} - seen
    for folder in sorted(missing):
        log(f"{folder:<24} no answer from the check at all")
    return ok and not missing


def verify_running_config(instance_id: str) -> bool:
    """Check the CA the live Caddy process is using, not the file we sent.

    Shipping a file and the process reading it are two different events. This
    asserts the second one actually happened.
    """
    step("Verifying the config Caddy is actually running")
    out = run_remote(
        instance_id,
        [
            "set -u",
            f"cd {APP_DIR}",
            "docker compose exec -T caddy wget -qO- http://127.0.0.1:2019/config/ 2>/dev/null"
            " | grep -o 'acme-staging-v02' | head -1 || true",
            "echo END_OF_CHECK",
        ],
        "verify running config",
        timeout=180,
        quiet=True,
    )
    running_staging = "acme-staging-v02" in out

    if running_staging == ACME_STAGING:
        log("staging" if ACME_STAGING else "production" + " — matches config.json")
        return True

    log("")
    log("MISMATCH between config.json and the running Caddy:")
    log(f"  config.json says  : {'staging' if ACME_STAGING else 'production'}")
    log(f"  Caddy is using    : {'staging' if running_staging else 'production'}")
    log("")
    log("The file was shipped but the process is running something else.")
    log("Recreating the Caddy container to force it.")
    run_remote(
        instance_id,
        [
            "set -eu",
            f"cd {APP_DIR}",
            "docker compose up -d --force-recreate --wait --wait-timeout 180 caddy",
        ],
        "force-recreate caddy",
        timeout=300,
    )
    return False


def rollback(instance_id: str) -> None:
    step("Rolling back to the previous release")
    run_remote(
        instance_id,
        [
            # SSM runs these with /bin/sh, which is dash on Ubuntu. dash has
            # no "pipefail", so keep every command POSIX.
            "set -eu",
            f"if [ ! -d {PREV_DIR} ]; then echo 'no previous release on disk'; exit 1; fi",
            f"rm -rf {APP_DIR} && cp -a {PREV_DIR} {APP_DIR}",
            f"cd {APP_DIR}",
            "docker compose up -d --build --remove-orphans --wait --wait-timeout 300",
            "docker compose exec -T caddy caddy reload "
            "--config /etc/caddy/Caddyfile --adapter caddyfile "
            "|| docker compose restart caddy",
            "docker compose ps --format '{{.Name}}\\t{{.State}}\\t{{.Status}}'",
        ],
        "rollback",
    )


# ----------------------------------------------------------------------------
# 9. Small operator commands
# ----------------------------------------------------------------------------

def show_status() -> None:
    inst = require_instance()
    step(f"Instance {inst['InstanceId']} — {inst['State']['Name']}")
    log(f"public IP: {inst.get('PublicIpAddress', '(none)')}")
    if inst["State"]["Name"] != "running":
        return
    run_remote(
        inst["InstanceId"],
        [
            f"cd {APP_DIR} 2>/dev/null || {{ echo 'nothing deployed yet'; exit 0; }}",
            "docker compose ps --format '{{.Name}}\\t{{.State}}\\t{{.Status}}'",
            "echo '--- disk ---'",
            "df -h / | tail -1",
            "echo '--- memory ---'",
            "free -h | head -2",
        ],
        "status",
    )


def show_logs(folder: str) -> None:
    inst = require_instance()
    step(f"Last 100 lines from {folder}")
    run_remote(
        inst["InstanceId"],
        [f"cd {APP_DIR}", f"docker compose logs --tail=100 --no-color {folder}"],
        f"logs {folder}",
        timeout=120,
    )


def _github_env(key: str, value: str) -> None:
    """Pass a fact to later workflow steps, when running inside Actions."""
    path = os.environ.get("GITHUB_ENV")
    if path:
        try:
            with open(path, "a") as fh:
                fh.write(f"{key}={value}\n")
        except OSError:
            pass


def off_switch_banner(instance_id: str, already_off: bool) -> None:
    step("THE SERVER IS OFF")
    log(f"instance : {instance_id}  ({'was already stopped' if already_off else 'just stopped'})")
    log("")
    log("YOUR SITES ARE DOWN. Nothing is answering on your domains.")
    log("This is not a fault — something asked for it.")
    log("")
    log("Still being charged while it is off:")
    log("  - the public IPv4 address, about $0.005/hour. Since February 2024")
    log("    AWS charges for every public IPv4 address, attached or idle, so")
    log("    stopping the server does not change this one either way.")
    log("  - the disk.")
    log("Not charged: the compute, which is the bulk of the bill.")
    log("")
    log("To pay nothing at all, --destroy instead of --stop. It rebuilds from")
    log("this repo in about ten minutes, but the new server gets a new IP, so")
    log("your DNS records have to follow it.")
    log("")
    log("To bring it back:")
    log('  set "server_off": false in config.json and push')
    log("  or Actions -> Run workflow -> action: start")
    log("")
    log("The containers restart by themselves on boot, so a start is enough.")
    log("You do not need to deploy again.")


def stop_server(quiet_if_off: bool = False) -> None:
    inst = find_instance()
    if not inst:
        log("No instance to stop.")
        return
    instance_id = inst["InstanceId"]
    state = inst["State"]["Name"]

    if state in {"stopped", "stopping"}:
        _github_env("MFD_SERVER_OFF", "true")
        off_switch_banner(instance_id, already_off=True)
        return

    step(f"Stopping {instance_id}")
    ec2.stop_instances(InstanceIds=[instance_id])
    log("waiting for it to stop")
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance_id])
    _github_env("MFD_SERVER_OFF", "true")
    off_switch_banner(instance_id, already_off=False)


def start_server() -> None:
    inst = find_instance()
    if not inst:
        fail(f"No instance tagged Name={SERVER_TAG}. Run a deploy to create one.")
    instance_id = inst["InstanceId"]
    state = inst["State"]["Name"]

    if state == "running":
        step(f"{instance_id} is already running")
        log(f"public IP: {inst.get('PublicIpAddress', '(none)')}")
        return

    step(f"Starting {instance_id}")
    ec2.start_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    public_ip = ensure_elastic_ip(instance_id, plan=False)

    step("Back on")
    log(f"public IP: {public_ip}")
    log("The containers restart on boot, so the sites should answer within a")
    log("minute. Nothing else to do — but remember to set \"server_off\": false")
    log("in config.json, or the next push will switch it off again.")


def destroy() -> None:
    step("Destroying")
    inst = find_instance()
    if inst:
        ec2.terminate_instances(InstanceIds=[inst["InstanceId"]])
        log(f"terminating {inst['InstanceId']}")
    for addr in ec2.describe_addresses(
        Filters=[{"Name": "tag:Project", "Values": [PROJECT]}]
    )["Addresses"]:
        ec2.release_address(AllocationId=addr["AllocationId"])
        log(f"released {addr['PublicIp']}")
    log("security group, role and profile were left alone — they cost nothing.")


# ----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Provision and deploy multi-folder-domain.")
    parser.add_argument("--plan", action="store_true", help="show what would change, change nothing")
    parser.add_argument("--status", action="store_true", help="what is running right now")
    parser.add_argument("--logs", metavar="FOLDER", help="last 100 log lines from one container")
    parser.add_argument("--rollback", action="store_true", help="put the previous release back")
    parser.add_argument("--stop", action="store_true", help="switch the server off (keeps everything)")
    parser.add_argument("--start", action="store_true", help="switch the server back on")
    parser.add_argument("--destroy", action="store_true", help="terminate the instance, release the IP")
    args = parser.parse_args()

    if args.destroy:
        return destroy()
    if args.stop:
        return stop_server()
    if args.start:
        return start_server()
    if args.status:
        return show_status()
    if args.logs:
        return show_logs(args.logs)
    if args.rollback:
        return rollback(require_instance()["InstanceId"])

    # The off switch wins over everything. Checked before any work is done, so
    # a run with it set never builds, ships or touches a container.
    if SERVER_OFF:
        log('config.json has "server_off": true')
        return stop_server()

    sites, problems = load_sites()
    step(f"Deploying {len(sites)} site(s)")
    for site in sites:
        names = ", ".join([site["domain"], *site["aliases"]])
        kind = "own Dockerfile" if site["own_dockerfile"] else "static (nginx)"
        log(f"{site['folder']:<20} {names:<40} [{kind}]")

    notready = [(f, w) for f, w, sev in problems if sev == "notready"]
    broken = [(f, w) for f, w, sev in problems if sev == "broken"]

    if notready:
        step(f"Ignored {len(notready)} folder(s) with no domain set")
        for folder, why in notready:
            log(f"{folder:<20} {why}")

    if broken:
        step(f"SKIPPED {len(broken)} folder(s) with problems — the rest still deploy")
        for folder, why in broken:
            log(f"{folder:<20} {why}")
        log("")
        log("A skipped folder is not deployed. If it was live before, its")
        log("container is removed and that domain stops answering.")

    if ACME_STAGING:
        log("")
        log("ACME_STAGING is ON — certificates will be untrusted test ones.")
        log("Turn it off once a real deploy has worked end to end.")

    sg_id = ensure_security_group(args.plan)
    profile = ensure_instance_profile(args.plan)
    inst = ensure_instance(sg_id, profile, args.plan)
    instance_id = inst["InstanceId"]
    public_ip = ensure_elastic_ip(instance_id, args.plan)

    caddyfile = build_caddyfile(sites)
    compose = build_compose(sites, caddyfile)

    unmanaged = (
        sync_dns(sites, public_ip, args.plan)
        if MANAGE_DNS
        else [n for s in sites for n in [s["domain"], *s["aliases"]]]
    )

    if args.plan:
        step("Caddyfile that would be written")
        print(caddyfile)
        step("docker-compose.yml that would be written")
        print(compose)
        step("Plan complete — nothing was changed.")
        if broken and FAIL_ON_SKIPPED:
            sys.exit(1)
        return

    wait_for_ssm(instance_id)
    wait_for_docker(instance_id)

    try:
        deploy_payload(instance_id, build_payload(sites, caddyfile, compose))
        verify_running_config(instance_id)
        healthy = health_check(instance_id, sites)
        reason = "" if healthy else "the new release did not come up healthy"
    except RemoteError as exc:
        healthy = False
        reason = "the deploy itself failed"
        print(f"\n{exc}", file=sys.stderr, flush=True)

    if not healthy:
        step(f"Rolling back — {reason}")
        try:
            rollback(instance_id)
            put_back = "The previous release has been put back."
        except Exception:
            put_back = (
                "WARNING: the rollback also failed. Run `python deploy.py --status` "
                "to see what state the server is in."
            )
        fail(
            f"Deploy aborted because {reason}. {put_back}\n"
            "Run `python deploy.py --logs FOLDER` to see why."
        )

    step("Done")
    log(f"instance : {instance_id}")
    log(f"public IP: {public_ip}")

    if unmanaged:
        print("\n  These names still need an A record pointing at that IP:\n")
        for name in unmanaged:
            print(f"    {name:<40} A    {public_ip}")
        print(
            "\n  Caddy asks for a certificate the first time a domain resolves here,\n"
            "  so HTTPS starts working a minute or two after DNS propagates.\n"
        )
    else:
        print("\n  All domains are pointed at the server. Nothing left to do.\n")

    if broken and FAIL_ON_SKIPPED:
        step("Finished, but one or more folders have problems")
        for folder, why in broken:
            log(f"{folder:<20} {why}")
        log("")
        log("The healthy sites are live. This run is marked failed so the")
        log("broken folders don't go unnoticed. Set \"fail_on_skipped\": false")
        log("in config.json if you would rather have a warning.")
        sys.exit(1)


if __name__ == "__main__":
    main()