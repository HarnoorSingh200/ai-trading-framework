# Deploying v3 on a scheduled Oracle Cloud machine

> The concrete plan for this tenancy, including the existing compartment,
> network, instance shape and S3 handoff, is in
> [`oracle-plan.md`](oracle-plan.md). This file describes the reusable pieces.

The deployed instance is `trading-framework-v3-2` in `remote-dev`, region
`ap-hyderabad-1`: Ubuntu 24.04, 1 OCPU / 4 GB, 50 GB boot volume, started
weekdays at **09:10 IST** by `trading-framework-v3-start`. Module 4 mirrors its
archive to `s3://your-archive-bucket/trading-framework/history.json`.

The shape these files build:

1. **Something starts the instance** a little before the trading day. On
   Oracle that is a **Resource Scheduler** schedule (the EC2 start/stop
   equivalent) - no code at all - or, if you want the serverless hop, a
   **Function** (the Lambda equivalent) that a schedule triggers.
2. **The instance starts the framework by itself.** `cloud-init` installs
   everything on the first boot; the systemd unit is enabled, so every boot
   after that is just "the machine comes up and the runner is running".
3. **The central process works the day** exactly as it does on any machine:
   Module 1 and Module 4 start together (reporting does not wait for market
   data to stabilise), Modules 3 and 2 run for the window once Module 1 is
   stable, and Module 2 is closed after the last decision.
4. **The machine turns itself off.** With `FRAMEWORK_SELF_SHUTDOWN=true`,
   once the close has settled and the day's book is archived, the runner
   stops every remaining service, asks its own supervisor whether anything is
   still running, and only then runs `FRAMEWORK_SHUTDOWN_COMMAND` (the deployed
   wrapper is `sudo /usr/local/sbin/trading-poweroff`). A service that will not
   die keeps the machine up and says so on Telegram - it never pulls the plug
   on something that might be mid-write.
5. **A failsafe covers the rest** (optional): if the runner never got that
   far - it never became ready, it died, a service refused to stop - a
   systemd timer powers the machine off at 15:15 IST on weekdays anyway.

Module 4 also mirrors its long archive to S3. The local
`data/history/history.json` remains the dashboard's read path; the S3 object is
the durable copy. The runner's final sync forces that upload, and a failed
upload keeps the machine up and sends a Telegram alert instead of powering
off. Configure `MODULE4_S3_BUCKET` and `MODULE4_S3_PREFIX` in
`/etc/trading-framework/framework.env`; keep the AWS profile in
`/etc/trading-framework/aws-credentials`.

The files:

| file | what it is |
|---|---|
| `trading-framework.service` | the systemd unit the runner runs |
| `trading-site.service` | the operator site (Vite dev server on loopback 5200) |
| `cloud-init.yaml` | first-boot install (paste into instance user data) |
| `cloud-init-trading.yaml` | OS/service-account preparation for the dedicated trading VM |
| `bootstrap.sh` | install/update on a live instance (idempotent) |
| `framework.env.example` | runtime, S3 and shutdown settings for the VM |
| `trading-shutdown.sudoers` | passwordless shutdown permission for `trading` |
| `trading-poweroff` | root wrapper for the unattended force-poweroff path |
| `framework-failsafe.service` / `.timer` | the 15:15 backstop, opt-in |

The site is served by `npm run dev` because that is what applies Vite's
proxies; it binds to loopback only. Reach it with
`ssh -L 5200:127.0.0.1:5200 ubuntu@<instance>` and open
`http://127.0.0.1:5200`. `cloud-init-trading.yaml` installs Node and npm for
it, and `bootstrap.sh` installs its dependencies and unit when updating.

## Oracle, in AWS words

| AWS | Oracle Cloud Infrastructure |
|---|---|
| EC2 | Compute (VM instances) |
| Lambda | OCI Functions |
| EventBridge scheduled rule | Resource Scheduler - it schedules Functions too (since Sept 2024) |
| EC2 start/stop schedule | Resource Scheduler `START_RESOURCE` / `STOP_RESOURCE` |
| user-data / cloud-init | instance metadata user data, run by cloud-init (max 32 KB) |

## What starts the machine

### A. Resource Scheduler starts the instance (recommended)

One schedule, no code, no extra credentials to manage. Console: **Governance
& Administration -> Resource Scheduler -> Create a schedule**, action
**Start**, resource = the instance, weekly, Monday-Friday. CLI:

```bash
oci resource-scheduler schedule create \
  --compartment-id <compartment-ocid> \
  --action START_RESOURCE \
  --recurrence-type CRON \
  --recurrence-details "10 9 * * mon-fri" \
  --resources '[{"id": "<instance-ocid>"}]' \
  --local-time-zone Asia/Kolkata
```

Two readings of the time, because the docs differ by surface: the CLI/API
accept `--local-time-zone` (IANA name), while some console forms evaluate cron
in UTC and say so. The example above is a **09:10 IST** start. If you enter the
cron in the console, make sure the schedule says IST rather than UTC. Check
`oci resource-scheduler schedule create --help` in your CLI version for the
exact flag shapes. The minimum interval Resource Scheduler accepts is 30-60
minutes (the docs differ), which a once-a-day schedule is far outside.

**The deployed environment also has a `STOP_RESOURCE` backstop at 15:00 IST**
because the Ubuntu logind poweroff path did not reliably transition the
instance state on this image. The runner still attempts its normal 14:50
shutdown first. The stop schedule is a hard safety net, not a replacement for
the runner's service shutdown and archive sync.

### B. A scheduled Function starts the instance (the Lambda shape)

Resource Scheduler can trigger a Function on the same kind of cron (choose
the **Function** resource type when creating the schedule; scheduled Functions
are invoked in detached mode). The function calls the Compute API:

```python
import oci

def handler(ctx, data: bytes = b""):
    compute = oci.core.ComputeClient(
        config={}, signer=oci.auth.signers.get_resource_principals_signer()
    )
    compute.instance_action("<instance-ocid>", "START")
    return {"started": True}
```

It needs an IAM policy letting the function's dynamic group act on the
instance (the broad grant is `manage instance-family`; the console's policy
builder offers the narrower instance-action permission). Remember the trigger
for the trigger: the schedule still lives in Resource Scheduler, so this extra
hop only pays for itself when the function actually does something more -
checking a holiday calendar, notifying a chat, starting a second machine.
Functions are free for the first 2 million invocations and 400,000
GB-seconds a month; one call a day is nothing.

## First-time setup (once)

1. **Push v3 somewhere the instance can reach.** v3 is currently untracked in
   its repository - commit it and push it (a private repo is fine; the
   instance just needs a deploy key or token). Fill `REPO_URL` in
   `cloud-init.yaml` with that URL. For the separate trading VM, use
   `cloud-init-trading.yaml`, which only prepares the operating system and
   service account; the code tree is copied over SSH.
2. **Create the instance.** Ubuntu 24.04 or Oracle Linux 9, shape
   `VM.Standard.A1.Flex` (Arm is fine - every dependency has an aarch64
   wheel). Paste `cloud-init.yaml` into the instance's "Initialization
   script" / user data box. Always Free gives the first 1,500 OCPU-hours and
   9,000 GB-hours a month, which is 2 OCPUs + 12 GB running continuously -
   more than the framework needs.
3. **Place the secrets once**, after the first boot:

   ```bash
   ssh ubuntu@<instance>
   sudo install -d -m 0750 -o trading -g trading /etc/trading-framework
   sudo tee /etc/trading-framework/secrets.env >/dev/null <<'EOF'
   ZERODHA_USER_ID=...
   ZERODHA_PASSWORD=...
   ZERODHA_TOTP_SECRET=...
   ZERODHA_API_KEY=...          # if you have one
   DEEPSEEK_API_KEY=...
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   EOF
   sudo chmod 600 /etc/trading-framework/secrets.env
   printf 'FRAMEWORK_SELF_SHUTDOWN=true\n' | sudo tee /etc/trading-framework/framework.env
   sudo systemctl start trading-framework
   sudo systemctl status trading-framework --no-pager
   ```

   The boot volume persists across stop/start, so this is a once-ever step:
   every later boot starts the framework by itself.
4. **Let the runner check itself once** (stop the unit first):

   ```bash
   sudo systemctl stop trading-framework
   sudo -u trading /opt/trading-framework-v3/.venv/bin/python -m runner --check
   sudo systemctl start trading-framework
   ```

   `--check` prints the resolved configuration and sends one Telegram
   message - the cheapest proof that the machine can reach the broker's host
   and the operator's phone.
5. **Create the start schedule** (section A). The first real day will boot,
   work, close and power itself off.
6. **Optional failsafe**, for the days the runner does not get that far:

   ```bash
   sudo cp deploy/framework-failsafe.service deploy/framework-failsafe.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now framework-failsafe.timer
   ```

   Disable it while debugging (`sudo systemctl disable --now
   framework-failsafe.timer` if you want the machine to stay up in the
   evening).

## The sudoers line

The runner runs as `trading` and must run the shutdown command without a
password:

```bash
echo 'trading ALL=(root) NOPASSWD: /sbin/shutdown, /sbin/poweroff, /usr/sbin/shutdown, /usr/sbin/poweroff, /usr/local/sbin/trading-poweroff' \
  | sudo tee /etc/sudoers.d/trading-shutdown
sudo chmod 440 /etc/sudoers.d/trading-shutdown
```

## Updating

```bash
sudo FRAMEWORK_REPO_URL=<repo-url> bash /opt/trading-framework-v3/deploy/bootstrap.sh
```

or by hand: `git pull`, `.venv/bin/pip install .`,
`sudo systemctl restart trading-framework`.

The current dedicated VM was installed from a local tree rather than a Git
clone. Update it by copying the new tree over SSH, preserving `module1/data/`
and the runtime `data/agents/` definitions, then reinstalling if dependencies
changed and restarting the service.

## What will surprise you

- **Boot to ready is about 2-4 minutes**: the boot itself, the broker
  sign-in, the history fetch (rate-limited by the broker) and the indicator
  pass. Start the VM at **09:10-09:15 IST** so Module 1 is stable before the
  runner starts Modules 3 and 2 at **09:25**. Starting at 09:25 is possible
  but tight; starting after 14:30 intentionally starts no trading services.
- **If the runner never becomes ready** (the broker is down, the machine was
  restarted late), it never reaches the close sequence, so it will not power
  the machine off by itself. That case is exactly why the failsafe timer
  exists.
- **One account, one machine.** A second sign-in for the same Zerodha user
  invalidates the first session. Do not keep a local copy running against the
  same account.
- **The machine has no public surface.** Everything binds to loopback; reach
  the site with an SSH tunnel (`ssh -L 5200:127.0.0.1:5200 ubuntu@<instance>`)
  or Tailscale. Never expose 5200-5205.
- **A stopped instance releases its ephemeral public IP** and gets a
  different one on the next start. The framework does not care (all its
  traffic is outbound), but your SSH config will; reserve an IP if that
  bothers you.
- **Oracle may reclaim idle Always Free compute.** A machine that runs part
  of the day and sleeps otherwise is not obviously safe from that policy;
  check the current terms for your tenancy, or use a paid shape (this
  workload costs well under a dollar a month of compute either way).
