# multi-folder-domain

One small EC2 instance. Every folder runs in its own Docker container. Caddy
sits in front, handles the certificates, and routes each domain to its
container.

```
multi-folder-domain/
├── deploy.py                  the whole orchestrator
├── config.json                every setting, in one committed file
├── requirements.txt
├── .github/workflows/deploy.yml
├── scripts/ci-iam-policy.json minimum permissions for the CI user
├── folder-one/                no Dockerfile → gets a generated nginx one
│   ├── site.json              { "domain": "one.example.com" }
│   ├── index.html
│   ├── index.css
│   └── script.js
└── folder-two/                has its own Dockerfile → that one is used
    ├── Dockerfile
    └── …
```

Adding a site is: copy a folder, change `site.json`, push.

---

## How it works

### State — AWS is the database

There is no state file. `deploy.py` asks EC2 for an instance tagged
`Name=multi-folder-domain-server` in a live state.

* running → reuse it
* stopped → start it
* missing → launch it, tag it, wait for it

Nothing is written back to the repo, so there is no bot-commit dance and no
file that can drift from reality. The workflow uses a `concurrency` group so
two pushes can't both decide the instance is missing and launch one each.

### One container per folder

The script generates a `docker-compose.yml` from the folder manifests and runs
it with `--build --remove-orphans --wait`.

* `--build` rebuilds only what changed
* `--remove-orphans` is what makes **deleting a folder actually delete its
  container** — without it, old sites keep running forever
* `--wait` makes the command block until every container reports healthy

Caddy deliberately has **no `depends_on`**. If it waited for every folder to
be healthy, a single bad Dockerfile would stop the proxy starting and take
every other site down with it. Caddy starts regardless, and a broken backend
returns 502 for its own domain only. Isolation is the entire point of running
these in separate containers; a dependency chain would hand it back.

A folder with no `Dockerfile` gets a generated one — `nginx:alpine` serving
the folder's files. A folder **with** a `Dockerfile` is left alone and that one
is built, so a folder can be a Node app, a Go binary, anything that listens on
a port. Set the port in `site.json`:

```json
{ "domain": "api.example.com", "port": 3000 }
```

Caddy follows it. See `folder-two/Dockerfile` for the pattern.

### Caddy and the domains

`build_caddyfile()` regenerates the **entire** Caddyfile every run — it never
appends. An appended config drifts the moment a folder is renamed or removed,
leaving Caddy renewing certificates for domains that no longer exist.

Caddy and the site containers share a private Docker network, so routing is
`reverse_proxy folder-one:80` — a container name, resolved by Docker. No host
ports, no port bookkeeping, nothing to collide.

Certificates live in a named volume, so recreating the Caddy container doesn't
throw them away.

The config ends with a catch-all returning 404, so a hostname pointed at the
IP but not configured here gets a flat 404 rather than being quietly served
whichever site happens to be first.

### Reaching the box — SSM, not SSH

Port 22 is **not** opened by default.

The instance makes an outbound connection to Systems Manager. The script ships
files and runs commands through `ssm:SendCommand`, so there is no inbound
administrative port and no key material anywhere in CI.

If you want SSH for yourself, set `admin_cidr` in `config.json` to your own
address with `/32`. The script refuses anything wider.

### Safety nets

**Health check.** After every deploy, each container is fetched from inside
the Caddy container, over the private network. This needs no DNS, no
certificate and no extra image, so it works on the very first deploy.

**Automatic rollback.** The previous release is kept at `/srv/mfd.prev`.
**Any** failure puts it back — the build failing, a container never coming up,
or the health check not answering. Not just the last of those.

**Two configs, two checks.** `docker compose config` validates the compose
file. It does not look at the Caddyfile, so a bad Caddyfile would crash-loop
the proxy — the deploy therefore runs `caddy validate` in a throwaway
container first, before anything live is touched.

**A half-written folder doesn't stop the others.** A folder with no domain
yet, a typo'd hostname, broken JSON, or no `index.html` is **skipped** — the
remaining sites deploy as normal. The skipped ones are listed in the output and
the run exits non-zero so it still shows red in Actions. Same principle as
Caddy not depending on every container: one unfinished folder must never hold
the rest hostage.

Two folders claiming the same domain is the one case that isn't a skip of just
one — **both** are dropped, because there is no way to know which should win
and guessing would silently serve the wrong site. That domain goes down
loudly; everything else carries on.

A skipped folder is not deployed, so if it was live before, its container is
removed and the domain stops answering. The repo is the source of truth.

**Bootstrap wait.** The SSM agent reports ready well before cloud-init has
finished installing Docker, so a first deploy can otherwise land on a server
with no `docker` command. The script waits for Docker to actually exist.

**Validation before AWS.** The workflow parses every `site.json` and rejects
duplicate domains *before* touching any infrastructure.

**Staging certificates.** Let's Encrypt limits how many certificates you can
request. `acme_staging` starts at `true` so you cannot get this wrong — the
certificates will be untrusted, but you cannot lock yourself out. Turn it off
once a real deploy succeeds end to end.

---

## Setup

**1 — Repository secrets** (Settings → Secrets and variables → Actions)

| name | value |
| --- | --- |
| `AWS_ACCESS_KEY` | access key id for the CI user |
| `AWS_SECRET_ACCESS_KEY` | its secret |

**2 — Settings** live in `config.json`, committed with the code. No repository
variables, nothing to remember in a web form.

```json
{
  "region": "us-east-1",
  "project_name": "multi-folder-domain",
  "instance_type": "t3.micro",
  "disk_gb": 20,
  "acme_email": "",
  "acme_staging": true,
  "manage_dns": false,
  "admin_cidr": ""
}
```

| key | what it does |
| --- | --- |
| `acme_staging` | `true` = test certificates, no rate-limit risk. **Start here.** |
| `acme_email` | expiry warnings from Let's Encrypt |
| `manage_dns` | `true` sets the A records in Route 53 for you |
| `admin_cidr` | your own IP with `/32` opens SSH. Empty = port 22 stays shut |
| `instance_type` | defaults to `t3.micro` |
| `disk_gb` | defaults to 20; Docker images need the room |
| `region` | the workflow reads this too, so it's set in one place |

An environment variable of the same name in CAPITALS beats the file, for a
one-off local run:

```bash
ACME_STAGING=false INSTANCE_TYPE=t3.small python deploy.py
```

A malformed `config.json` stops the run before anything is created, naming the
key that is wrong.

If the repo is public, leave `admin_cidr` empty — SSH is off by default and
your home IP is nobody's business.

**3 — Set your domains** in each `site.json`, then push to `main`.

**4 — DNS.** With `MANAGE_DNS=true` and the domain in a Route 53 public hosted
zone, the script sets the A record itself and there is nothing to do. Anything
not in Route 53 is printed at the end of the run for you to set by hand:

```
one.example.com        A    52.x.x.x
```

Certificates are issued a minute or two after DNS resolves. A TLS error right
after the first deploy usually means DNS hasn't propagated — wait rather than
re-running, because repeated failures count against the rate limits.

**5 — Set `acme_staging` to `false`** in `config.json` once a real deploy has
worked, and push again.

The IP is an Elastic IP tagged to this project, so it survives a stop/start and
your DNS records stay correct.

---

## Running it

From GitHub: Actions → deploy → *Run workflow*, and pick `deploy`, `plan`,
`status` or `rollback`. Put a folder name in the logs box to fetch that
container's logs instead.

Locally:

```bash
pip install -r requirements.txt
export AWS_REGION=us-east-1

python deploy.py --plan             # read-only; prints the Caddyfile and compose file
python deploy.py                    # provision and deploy
python deploy.py --status           # containers, disk, memory
python deploy.py --logs folder-one  # last 100 lines from one container
python deploy.py --rollback         # put the previous release back
python deploy.py --destroy          # terminate the instance, release the IP
```

`--plan` still needs read credentials — it describes real resources to work out
what it would change.

---

## Adding a site

```bash
cp -r folder-one folder-three
# edit folder-three/site.json → "domain": "three.example.com"
git add folder-three && git commit -m "add folder-three" && git push
```

With `MANAGE_DNS=true`, that is the whole job. Without it, add the A record
the run prints at the end.

Deleting a site is `git rm -r folder-three` and a push. `--remove-orphans`
stops its container and the regenerated Caddyfile drops its domain.

---

## Things worth knowing

**Costs.** One `t3.micro`, a 20 GB gp3 volume, one Elastic IP that stays
attached. AWS free-tier terms have changed over time and differ between new and
older accounts, so check what applies to yours rather than assuming. An Elastic
IP bills whenever it is *not* attached to a running instance, so stopping the
box to save money partly backfires.

**Memory.** A `t3.micro` has 1 GB of RAM, and Docker builds will be killed by
the kernel partway through without help. The bootstrap adds a 2 GB swap file.
If you start building heavy images, move to `t3.small`.

**Disk.** Old images are pruned weekly by a cron job the bootstrap installs.
`--status` shows you the disk.

**Growing past the inline limit.** Assets ship inside the SSM command as
base64, which caps the payload at about 60 KB. The example repo packs to
roughly 4 KB. Small static folders are what this is for. If you outgrow it:
push the tarball to a private S3 bucket, add `s3:GetObject` on that bucket to
the instance role, and replace the `echo … | base64 -d` line with `aws s3 cp`.
Everything else stays as it is.

**Upgrading away from long-lived keys.** Static access keys in repository
secrets never expire and can't be scoped to a branch. GitHub can federate into
an IAM role with OIDC instead, issuing short-lived credentials per run with no
stored secret. Create an IAM OIDC provider for
`token.actions.githubusercontent.com`, create a role trusting your repo, add
`id-token: write` to the workflow's `permissions`, and swap the env block for
`aws-actions/configure-aws-credentials@v4` with `role-to-assume`. `deploy.py`
needs no changes — boto3 reads whatever the environment provides.

**The instance is cattle, not a pet.** Everything on it is rebuilt from this
repo on every run. If it gets into a strange state, terminate it and push
again. The only thing lost is the certificate cache, which is exactly why you
want `acme_staging` on while you are still experimenting.

**What this is not.** One instance means one point of failure — if it dies, the
sites are down until it comes back. That is the right trade for small personal
sites and the wrong one for anything with customers waiting.
