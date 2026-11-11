# Trading Framework v3 - Deployment Structure & Runbook

This document captures the complete architectural and operational blueprint of the production deployment of **Trading Framework v3** on **Oracle Cloud Infrastructure (OCI)**.

---

## 1. Cloud Infrastructure & Resource Inventory

### 1.1 OCI Tenancy & Compartment
* **Tenancy OCID**: `ocid1.tenancy.oc1..exampletenancyocid`
* **Region**: `ap-hyderabad-1`
* **Compartment**: `remote-dev`
* **Compartment OCID**: `ocid1.compartment.oc1..examplecompartmentocid`
* **Virtual Cloud Network (VCN)**: `remote-dev-vcn`
* **Subnet**: `remote-dev-subnet` (Availability Domain `AD-1`)

### 1.2 Compute Instances
| Instance Name | OCID | Shape & Specs | State | Purpose |
|---|---|---|---|---|
| **`trading-framework-v3-2`** | `ocid1.instance.oc1.ap-hyderabad-1.exampleinstanceocid` | `VM.Standard.E4.Flex`<br/>1 OCPU, 4 GB RAM, 50 GB Boot Volume | `STOPPED` | **Primary production VPS** running runner, modules 1-4, and operator UI |
| **`trading-framework-v3`** | `ocid1.instance.oc1.ap-hyderabad-1.exampleinstanceocid` | `VM.Standard.E4.Flex`<br/>1 OCPU, 4 GB RAM, 50 GB Boot Volume | `STOPPED` | Original instance superseded after network issue; preserved as fallback boot volume |
| **`remote-dev`** | `ocid1.instance.oc1.ap-hyderabad-1.exampleinstanceocid` | `VM.Standard3.Flex` | `RUNNING` | Independent development VM (unrelated to trading) |

### 1.3 Automated Scheduling (OCI Resource Scheduler)
| Schedule Name | OCID | Recurrence (IST) | Action | Target Resource | Current State |
|---|---|---|---|---|---|
| **`trading-framework-v3-start`** | `ocid1.resourceschedule.oc1.ap-hyderabad-1.examplescheduleocid` | Mon–Fri 09:10 IST (`10 9 * * mon-fri`) | `START_RESOURCE` | `trading-framework-v3-2` | **`INACTIVE`** (Deactivated) |
| **`trading-framework-v3-stop`** | `ocid1.resourceschedule.oc1.ap-hyderabad-1.examplescheduleocid` | Mon–Fri 15:00 IST (`0 15 * * mon-fri`) | `STOP_RESOURCE` | `trading-framework-v3-2` | **`INACTIVE`** (Deactivated) |

### 1.4 IAM Policies
Resource Scheduler requires explicit IAM policies to perform actions on instances in the compartment:
```text
Allow any-user to manage instance in compartment id ocid1.compartment.oc1..examplecompartmentocid
where all {
  request.principal.type='resourceschedule',
  request.principal.id='ocid1.resourceschedule.oc1.ap-hyderabad-1.examplescheduleocid'
}

Allow any-user to manage instance in compartment id ocid1.compartment.oc1..examplecompartmentocid
where all {
  request.principal.type='resourceschedule',
  request.principal.id='ocid1.resourceschedule.oc1.ap-hyderabad-1.examplescheduleocid'
}
```

---

## 2. In-Host Architecture & File Layout

### 2.1 Users and File Permissions
* **Service Account**: `trading:trading` (`/home/trading`)
* **Application Root**: `/opt/trading-framework-v3`
* **Python Environment**: `/opt/trading-framework-v3/.venv` (Python 3.12+)
* **Token Cache Directory**: `/var/lib/trading-framework` (mode `0750`, owner `trading:trading`)
* **Secrets Directory**: `/etc/trading-framework/` (mode `0750`, owner `root:trading`)

### 2.2 Configuration & Secrets Layout (Out-of-Tree)
No secrets exist inside git. On the VM, the following files are provisioned under `/etc/trading-framework/`:
1. **`secrets.env`** (mode `0640` `root:trading`):
   ```bash
   ZERODHA_USER_ID=...
   ZERODHA_PASSWORD=...
   ZERODHA_TOTP_KEY=...
   OPENAI_API_KEY=...
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
2. **`framework.env`** (mode `0640` `root:trading`):
   ```bash
   TRADING_ENV=production
   FRAMEWORK_SECRETS_FILE=/etc/trading-framework/secrets.env
   FRAMEWORK_SELF_SHUTDOWN=true
   FRAMEWORK_SHUTDOWN_COMMAND=sudo /usr/local/sbin/trading-poweroff
   FRAMEWORK_TOKEN_CACHE=/var/lib/trading-framework/token_cache.json
   AWS_SHARED_CREDENTIALS_FILE=/etc/trading-framework/aws-credentials
   AWS_DEFAULT_REGION=ap-southeast-1
   MODULE4_S3_BUCKET=your-archive-bucket
   MODULE4_S3_PREFIX="stock market v3"
   MODULE4_S3_REGION=ap-southeast-1
   ```
3. **`aws-credentials`** (mode `0600` `root:trading`):
   ```ini
   [default]
   aws_access_key_id=...
   aws_secret_access_key=...
   ```

### 2.3 Systemd Services & Timers
1. **`/etc/systemd/system/trading-framework.service`**:
   * Service for `runner/__main__.py`.
   * `User=trading`, `WorkingDirectory=/opt/trading-framework-v3`.
   * `ExecStart=/opt/trading-framework-v3/.venv/bin/python -m runner`.
   * `Restart=always`, `KillMode=control-group`, `TimeoutStopSec=90`.
2. **`/etc/systemd/system/trading-site.service`**:
   * Service for the Vite operator UI.
   * `User=trading`, `WorkingDirectory=/opt/trading-framework-v3/ui`.
   * `ExecStart=/usr/bin/npm run dev` (binds to `127.0.0.1:5200`).
3. **`/etc/systemd/system/framework-failsafe.service` & `.timer`**:
   * Runs at 15:15 IST as an in-guest backstop to power off the machine if the runner stalls.
4. **`/usr/local/sbin/trading-poweroff` & Sudoers**:
   * Root wrapper running `/sbin/poweroff -f`.
   * `/etc/sudoers.d/trading-shutdown` grants `trading ALL=(root) NOPASSWD: /usr/local/sbin/trading-poweroff`.

---

## 3. Operational Cycle (The Trading Day)

```
09:10 IST  -> OCI Resource Scheduler starts trading-framework-v3-2
09:12 IST  -> VM boots; systemd launches trading-framework.service & trading-site.service
09:15 IST  -> Module 1 auto-signs into Zerodha, loads instruments, seeds history, connects feed
              Module 4 starts and loads local/S3 historical ledger
09:25 IST  -> Module 1 indicators ready (>=90%); runner starts Module 3, then restarts Module 2 fleet
09:30 IST  -> Trading decision window opens (5m/15m candle cycles, tool execution, paper orders)
14:30 IST  -> Trading decision window closes
14:32 IST  -> Module 2 cleanly stopped after grace period
14:50 IST  -> Module 3 expires open orders, forces closing of remaining positions
14:50:20   -> Runner flushes Module 4 history to S3 (s3://your-archive-bucket/trading-framework/history.json),
              stops all remaining services, checks process tree, and executes trading-poweroff
15:00 IST  -> OCI Resource Scheduler STOP_RESOURCE acts as hard backstop
15:15 IST  -> Guest framework-failsafe.timer acts as secondary backstop
```

---

## 4. How to Reactivate / Resume Deployment

To resume the automated schedule:
```python
import oci
config = oci.config.from_file("~/.oci/config", "DEFAULT")
client = oci.resource_scheduler.ScheduleClient(config)

start_sched_id = "ocid1.resourceschedule.oc1.ap-hyderabad-1.examplescheduleocid"
stop_sched_id = "ocid1.resourceschedule.oc1.ap-hyderabad-1.examplescheduleocid"

client.activate_schedule(start_sched_id)
client.activate_schedule(stop_sched_id)
```

To manually start the instance for inspection or updates:
```python
compute = oci.core.ComputeClient(config)
instance_id = "ocid1.instance.oc1.ap-hyderabad-1.exampleinstanceocid"
compute.instance_action(instance_id, "START")
```
Access via SSH once started:
```powershell
ssh -i ~/.ssh/trading-framework -L 5200:127.0.0.1:5200 ubuntu@<assigned-ip>
```
