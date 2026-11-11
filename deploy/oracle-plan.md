# Oracle deployment plan

This is the concrete plan for running v3 on the Oracle tenancy configured at
`~/.oci`. It assumes one persistent VM that is stopped most of the
day, not a newly created VM every morning.

## Current OCI state

- Region: `ap-hyderabad-1`
- Existing compartment: `remote-dev`
- Existing VCN: `remote-dev-vcn`
- Existing subnet: `remote-dev-subnet`, availability domain `AD-1`

The old `remote-dev` instances are terminated. The original
`trading-framework-v3` instance was replaced after a stop/start left it
unreachable at the OS level; it remains stopped as the old boot volume. The
active deployment is `trading-framework-v3-2`.

### Provisioned

The active instance was **STOPPED** at the 2026-10-22 close and was started
manually for the **2026-10-23 deployment**; it is left **RUNNING** for
inspection, with the weekday 09:10 start / 15:00 stop schedule unchanged:

- Name: `trading-framework-v3-2`
- Shape: `VM.Standard.E4.Flex`, 1 OCPU / 4 GB
- Image: Canonical Ubuntu 24.04
- Boot volume: 50 GB, persistent
- Address: `<vm-public-ip>` (OCI-assigned; may change after a stop/start)
- Schedule: active Resource Scheduler, `10 9 * * mon-fri`, `Asia/Kolkata`
  (09:10 IST)
- Hard-stop backstop: active Resource Scheduler, `0 15 * * mon-fri`,
  `Asia/Kolkata` (15:00 IST)
- systemd: `trading-framework.service` enabled
- Failsafe: `framework-failsafe.timer` enabled at 15:15 IST

At the 2026-10-20 update, both schedules were active:

- `trading-framework-v3-start`: next run **2026-10-20 09:10 IST**
- `trading-framework-v3-stop`: next run **2026-10-20 15:00 IST**

The original scheduled start on 2026-10-19 failed with `404` because the
schedule's service principal had no IAM permission. That policy was added
afterward and the replacement instance was used to verify the configuration.
The OS-level poweroff path on this image did not reliably transition the OCI
instance state, so the explicit 15:00 OCI `STOP_RESOURCE` schedule is now the
backstop; the runner still performs its normal 14:50 sync/stop/poweroff.

Resource Scheduler runs as its own service principal. The schedule needs an
IAM policy in addition to the user's ability to create it. The policy used is:

```text
Allow any-user to manage instance in compartment id <remote-dev-compartment>
where all {
  request.principal.type='resourceschedule',
  request.principal.id='<schedule-ocid>'
}
```

Without that policy the schedule's first run succeeds as a schedule job but
the instance action fails with `404`/not authorized. The policy
`trading-framework-resource-scheduler` is active.
The stop schedule has its own equivalent policy,
`trading-framework-resource-scheduler-stop`, scoped to the stop schedule OCID.

The code was installed from the local tree at `/opt/trading-framework-v3`, the
virtualenv was built there, secrets and the AWS profile were placed under
`/etc/trading-framework`, and `runner --check` successfully sent the Telegram
test on the replacement instance.

The **2026-10-23 deployment** replaced the code tree from the local working
tree and wrote `/opt/trading-framework-v3/DEPLOYED_REVISION`. It shipped the
async Module 2 engine, gpt-6-luna through the Responses API with reasoning set
to high, the 50-word decision reason, token-usage mirroring into
`data/history/tokens.json`, the NIFTY 50 benchmark route (Module 1's
`/v1/market/benchmark`, mirrored by Module 4), the agent `enabled` switch
(nifty-intraday is deployed disabled), and the operator site as
`trading-site.service` on loopback 5200. Node and npm are installed by
`cloud-init-trading.yaml`; the site's dependencies are installed by
`npm ci` on update. `OPENAI_API_KEY` was added to
`/etc/trading-framework/secrets.env` (mode 0640 root:trading); module
definitions were copied separately because the tree's `data/` is deliberately
not shipped. A pre-market smoke test reached `history:
ready`, 50/50 instruments, feed connected, and 400 indicator frames; Module 4
loaded the 55-order S3 archive.

SSH access uses the dedicated key:

```powershell
ssh -i ~/.ssh/trading-framework ubuntu@<vm-public-ip>
```

The address is OCI-assigned and may change after a stop/start; read it from
the instance's VNIC in the OCI console if the command stops connecting.

## Instance

Recommended:

- Image: Ubuntu 24.04
- Shape: `VM.Standard.E4.Flex`, 1 OCPU / 4 GB, or an equivalent A1 Flex shape
- Boot volume: 50 GB, persistent
- Availability domain: same as the existing subnet (`AD-1`)
- No inbound application ports. The UI and module ports stay on loopback;
  reach the site through SSH port forwarding or a private network.

The existing subnet's security list currently allows SSH (22) from
`0.0.0.0/0`. Restrict that rule to the operator's address before this instance
is created. Do not open 5200-5205.

The boot volume holds:

- `/opt/trading-framework-v3` - code and virtualenv
- `/etc/trading-framework` - secrets, environment, AWS profile
- `/var/lib/trading-framework` - broker token cache
- `/opt/trading-framework-v3/data` - local Module 4 history and agent state

This is why the instance should be **stopped**, never terminated.

## Code updates

Do not pull code from S3 on every boot.

- First install: clone the repository once, or copy the tree over SSH.
- Later updates: `git pull` / `rsync` the tree and reinstall only when
  dependencies changed.
- Record the deployed revision in a small file such as
  `/opt/trading-framework-v3/DEPLOYED_REVISION`.
- If packaging by `tar`, exclude the runtime `data/` and `logs/` at the project
  root but **never exclude `module1/data/`**: it holds the checked-in NIFTY 50
  constituent list and the holiday seed.

The existing `deploy/cloud-init.yaml` clones a Git repository on first boot.
That is fine for the one-time install; it is not a daily code-delivery
mechanism.

## How this instance was deployed

The deployed instance was built from the local working tree over SSH:

```powershell
# Package the source, excluding runtime state.
tar -czf trading-framework-v3.tar.gz `
  --exclude=./.venv --exclude=./data --exclude=./logs `
  --exclude=./ui/node_modules --exclude=./ui/dist `
  -C "C:\path\to\Stock market v3" .

scp -i ~/.ssh/trading-framework `
  trading-framework-v3.tar.gz ubuntu@<vm-address>:/tmp/
```

On the VM:

```bash
sudo useradd -m -s /bin/bash trading
sudo install -d -m 0750 -o trading -g trading \
  /opt/trading-framework-v3 /etc/trading-framework /var/lib/trading-framework
sudo tar -xzf /tmp/trading-framework-v3.tar.gz -C /opt/trading-framework-v3
sudo chown -R trading:trading /opt/trading-framework-v3

sudo -u trading python3 -m venv /opt/trading-framework-v3/.venv
sudo -u trading /opt/trading-framework-v3/.venv/bin/python -m pip install \
  /opt/trading-framework-v3
```

The important packaging caveat is that the runtime `data/` and `logs/` are
excluded, but `module1/data/` must still be present after extraction. It holds
the NIFTY 50 constituent list and the holiday seed. The first deployment
missed it and Module 1 failed to start until it was copied separately.

Agent definitions are also runtime data, not code:

```text
/opt/trading-framework-v3/data/agents/nifty-intraday/definition.json
/opt/trading-framework-v3/data/agents/asd/definition.json
```

Both were copied after the initial install so Module 2 had a fleet to start.
Agent state, activity and decisions were left to the VM; Module 4's archive is
loaded from S3.

Secrets and the environment were placed under `/etc/trading-framework`:

```text
secrets.env       broker, DeepSeek, OpenAI and Telegram credentials
framework.env     runtime settings and S3/Module 4 settings
aws-credentials   AWS profile for the S3 mirror
```

All are mode `0640`, owned `root:trading`, and live outside the repository.
The AWS profile is the only place the S3 key is stored on the VM.

The service, shutdown permission, and failsafe were installed as:

```bash
sudo cp deploy/trading-framework.service /etc/systemd/system/
sudo install -m 0440 -o root -g root deploy/trading-shutdown.sudoers \
  /etc/sudoers.d/trading-shutdown
sudo cp deploy/framework-failsafe.service \
  deploy/framework-failsafe.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trading-framework.service
sudo systemctl enable --now framework-failsafe.timer
```

The Telegram check and the pre-market health check were run with:

```bash
cd /opt/trading-framework-v3
sudo -u trading env FRAMEWORK_SECRETS_FILE=/etc/trading-framework/secrets.env \
  FRAMEWORK_SELF_SHUTDOWN=true \
  /opt/trading-framework-v3/.venv/bin/python -m runner --check
```

The schedule principal policy was then created as described below, and the
VM was stopped, ready for the weekday 09:10 IST schedule.

## S3 archive

The bucket already exists: `your-archive-bucket`.
Module 4 uses only this prefix:

```text
s3://your-archive-bucket/trading-framework/history.json
```

The archive has been round-tripped through that exact object; the latest
observed object contained 55 orders across 2026-10-05 and 2026-10-19, written
at 14:50:23 IST on 2026-10-19.

Module 4 behavior:

1. On start, merge the local `data/history/history.json` with the S3 object.
2. Keep the local file as the dashboard's read path.
3. Upload the merged archive after a successful Module 3 sync.
4. The runner's final `/v1/sync` forces a final upload before shutdown.
5. If the S3 upload fails, the runner does not power off the VM. It sends a
   Telegram alert and leaves the machine up for recovery.

AWS credentials live only on the instance:

```text
/etc/trading-framework/aws-credentials   # [default] profile, mode 0600
```

The instance's environment must point at it:

```text
AWS_SHARED_CREDENTIALS_FILE=/etc/trading-framework/aws-credentials
AWS_DEFAULT_REGION=ap-southeast-1
MODULE4_S3_BUCKET=your-archive-bucket
MODULE4_S3_PREFIX="stock market v3"
MODULE4_S3_REGION=ap-southeast-1
```

No AWS key belongs in the repository, `cloud-init`, or the agent prompts.

Quote values containing spaces in `framework.env`. systemd's environment-file
parser is stricter than a shell; an unquoted
`MODULE4_S3_PREFIX=stock market v3` is not a safe assignment.
The same applies to the shutdown command:

```text
FRAMEWORK_SHUTDOWN_COMMAND=sudo /usr/local/sbin/trading-poweroff
```

`trading-poweroff` is a root-run wrapper that executes `/sbin/poweroff -f`.
Keeping the command in a wrapper avoids putting arguments through systemd's
environment-file parser, and the force path avoids the interactive logind
authentication that a plain `systemctl poweroff` can request on this image.

## Start schedule

The deployed OCI **Resource Scheduler** schedule is:

- Action: `START_RESOURCE`
- Resource: the `trading-framework-v3-2` instance
- Recurrence: Monday-Friday
- Timezone: `Asia/Kolkata`
- Start time: **09:10 IST** (the configured cron is `10 9 * * mon-fri`)

Use 09:10 if the machine has been stopped for a while and boot plus Module 1
startup is consistently close to 3-4 minutes. The runner's existing lead time
then starts Modules 3 and 2 at **09:25**, so a fresh 5-minute agent is alive for
the 09:30 candle.

Starting the VM at 09:25 is possible but tight: boot, broker sign-in, history,
feed and indicators may not be ready by 09:30. If Modules 2 and 3 are started
at exactly 09:30, a fresh 5-minute agent waits for 09:35 and misses the 09:30
decision.

If the exact 09:30 module start is a hard requirement, set
`FRAMEWORK_DAY_LEAD_SECONDS=0` and keep the VM start at 09:25. The runner will
start Modules 3 and 2 as soon as Module 1 is ready, which may be a little after
09:30. The first 5-minute decision will then be the 09:35 candle, not the 09:30
one. This is a deliberate trade: the five-minute lead exists to make the first
candle decidable.

The deployed environment also has a second Resource Scheduler entry:

- Action: `STOP_RESOURCE`
- Resource: `trading-framework-v3-2`
- Recurrence: `0 15 * * mon-fri`, `Asia/Kolkata` (15:00 IST)

This is a hard-stop backstop, not the normal close. The runner still performs
its 14:50 sync, service shutdown and poweroff first. The OCI stop schedule
exists because the Ubuntu/logind poweroff path can fail to transition the
instance state on this image.

## The day

- 09:15-09:25: VM boot, Module 1 sign-in, history, warm-up, indicators.
- 09:25: runner starts Module 3, then restarts Module 2.
- 09:30-14:30: decisions.
- 14:32:30: Module 2 stopped after the last decision grace.
- 14:50: Module 3 closes open positions by timeout; pending orders expire.
- 14:50:20: runner syncs Module 4, forces the S3 archive, stops all remaining
  services, and runs `sudo /usr/local/sbin/trading-poweroff` (force poweroff).

The order manager stores `session_close_at` on every order, so a position
cannot outlive the framework's 14:50 close.

## Holidays and late starts

A holiday, weekend, or a runner started after 14:30 has no trading day. It
still runs Module 1 and Module 4 - started together at boot, because reporting
does not wait for market data to stabilise - then the runner powers the VM down
if `FRAMEWORK_SELF_SHUTDOWN=true`.

The Control tab and Telegram show the reason: holiday name, weekend, or
decision window closed.

Keep the systemd failsafe timer at 15:15 as a last resort for a runner that
never becomes ready. It is a backstop, not the normal shutdown path.

## One-time setup checklist

1. Create `trading-framework-v3-2` in `remote-dev` / `remote-dev-vcn` /
   `remote-dev-subnet`.
2. Install Ubuntu 24.04, Python 3.12+, `python3-venv`, `git`, and
   `ca-certificates`.
3. Create the `trading` service user.
4. Put the code under `/opt/trading-framework-v3` and create the virtualenv.
5. Install `deploy/trading-framework.service`.
6. Create `/etc/trading-framework/secrets.env` with broker, model and
   Telegram credentials.
7. Create `/etc/trading-framework/framework.env` with
   `FRAMEWORK_SELF_SHUTDOWN=true` and the S3/Module 4 settings.
8. Create `/etc/trading-framework/aws-credentials` with the S3 profile.
9. Add the passwordless sudoers rule for the shutdown command.
10. Run `python -m runner --check` and confirm the Telegram test message.
11. Start the service, confirm `/health` and `/v1/status` on loopback.
12. Create the Resource Scheduler start and stop schedules.
13. Verify one full day: S3 object updated, Telegram "Market is off" message,
    all child processes gone, VM stopped.

## Validation before the first live-paper day

- `uv run pytest` (or the project virtualenv equivalent)
- `uv run ruff check .`
- `npm run build` in `ui/`
- `python -m runner --check`
- Direct S3 round-trip:
  `s3://your-archive-bucket/trading-framework/history.json`
- Confirm the instance's public ports are not exposed.
- Confirm the broker token cache is outside the repository.

## Resolved decision

The deployment created a separate `trading-framework-v3-2` VM so development
and trading state stay separate. The original `trading-framework-v3` was
stopped and superseded after its boot/network path became unreachable; it is
kept only as the old boot volume.
