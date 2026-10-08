# Production Deployment Manual: Bybit V5 Autonomous Engine on GCP

## Architectural Blueprint & Latency Topology

This manual details the step-by-step production rollout of the **Bybit V5 Autonomous Quantitative Engine** on **Google Cloud Platform (GCP)**.

```
+---------------------------------------------------------------------------------+
|                       GOOGLE CLOUD PLATFORM (GCP)                               |
|                  Region: asia-southeast1 (Singapore - Jurong)                   |
|                                                                                 |
|  +---------------------------------------------------------------------------+  |
|  |  Compute Instance (c3-standard-4 / e2-standard-2, Debian 12 Minimal)     |  |
|  |                                                                           |  |
|  |  [Static External IPv4] ──> Bound to Bybit API Whitelist                 |  |
|  |                                                                           |  |
|  |  +---------------------------------------------------------------------+  |  |
|  |  |  Systemd Service: bybit-engine.service (24/7 Watchdog Auto-Restart)  |  |  |
|  |  |                                                                     |  |  |
|  |  |  * Async WebSocket (Public L2 & Private Execution)                   |  |  |
|  |  |  * Multi-Timeframe Feature Engine (1m, 5m, 15m)                      |  |  |
|  |  |  * Market Regime Classifier (Trend vs Chop Filter)                  |  |  |
|  |  |  * Maker-First Post-Only Bracket Execution                           |  |  |
|  |  |  * SQLite Journal & Self-Adaptive Tuner                             |  |  |
|  |  +---------------------------------------------------------------------+  |  |
|  +--------------------------------------┬------------------------------------+  |
+-----------------------------------------┼---------------------------------------+
                                          │ Sub-10ms Cross-Cloud Interconnect
                                          │ (Equinix SG1 / Global Switch SG)
                                          ▼
+---------------------------------------------------------------------------------+
|                         AMAZON WEB SERVICES (AWS)                               |
|                      Region: ap-southeast-1 (Singapore)                         |
|                                                                                 |
|         Bybit Primary Core Matching Engines (api.bybit.com / stream.bybit.com)  |
+---------------------------------------------------------------------------------+
```

---

## 1. Cloud Infrastructure & Region Selection

Bybit's primary matching engines and WebSocket gateways are co-located in **AWS Singapore (`ap-southeast-1`)**. Deploying your trading instance in GCP's **`asia-southeast1` (Singapore)** region routes traffic through Singapore's direct internet exchanges (IXPs), typically achieving **sub-10ms round-trip latency (RTT)** to Bybit.

### Recommended GCP Compute Profiles

| Tier | GCP Machine Type | vCPU | RAM | Network Bandwidth | Use Case |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Enterprise HFT** | `c3-standard-4` | 4 (Intel Emerald Rapids) | 16 GB | Up to 10 Gbps (Tier_1) | Lowest latency, high-frequency orderbook parsing |
| **Compute Optimized**| `c2-standard-4` | 4 (Intel Cascade Lake) | 16 GB | Up to 10 Gbps | High sustained single-thread clock speed (3.8 GHz) |
| **Standard Production** | `e2-standard-2` | 2 (Shared) | 8 GB | Up to 4 Gbps | Cost-effective, reliable 24/7 autonomous scalping |

---

## 2. Step-by-Step Provisioning via `gcloud` CLI

Run these commands using the Google Cloud SDK (`gcloud`) or Cloud Shell.

### Step 2.1: Set Environment Variables
```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="asia-southeast1"
export ZONE="asia-southeast1-a"
export INSTANCE_NAME="bybit-trading-prod-01"
export STATIC_IP_NAME="bybit-bot-static-ip"

gcloud config set project $PROJECT_ID
gcloud config set compute/region $REGION
gcloud config set compute/zone $ZONE
```

### Step 2.2: Reserve a Static External IPv4 Address
> **CRITICAL**: Bybit API keys should always be locked to a fixed whitelisted IP. A static external IP ensures your connection is never blocked due to dynamic IP rotation.

```bash
gcloud compute addresses create $STATIC_IP_NAME \
    --region=$REGION

# Retrieve reserved IP address
export STATIC_IP=$(gcloud compute addresses describe $STATIC_IP_NAME --region=$REGION --format="value(address)")
echo "Reserved Static IP: $STATIC_IP"
```

### Step 2.3: Provision the Compute Engine Instance
```bash
gcloud compute instances create $INSTANCE_NAME \
    --zone=$ZONE \
    --machine-type=c3-standard-4 \
    --address=$STATIC_IP \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=50GB \
    --boot-disk-type=pd-ssd \
    --network-tier=PREMIUM \
    --metadata=enable-oslogin=TRUE \
    --tags=trading-engine
```

### Step 2.4: Tighten Firewall Security
Ensure all inbound traffic except authenticated SSH is blocked.
```bash
# Allow SSH via Google Cloud Identity-Aware Proxy (IAP) or OS Login
gcloud compute firewall-rules create allow-ssh-trading \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:22 \
    --target-tags=trading-engine
```

---

## 3. Bybit API Key Setup & IP Whitelisting

1. Log into your [Bybit Account](https://www.bybit.com).
2. Navigate to **Account & Security** -> **API Management** -> **Create New Key**.
3. Select **System-generated API Keys**.
4. Set Key Usage: **API Transaction**.
5. Set Name: `GCP-Singapore-UTA-Engine`.
6. Select **Read-Write** permissions:
   - **Unified Trading**: Check `Orders`, `Positions`, `Account Transfer`.
   - (For Standard Accounts: Check `Contract - Orders`, `Contract - Positions`).
7. **IP Whitelisting**: Select **"Only IPs with permissions granted can access this API"**.
8. Paste the `$STATIC_IP` obtained in Step 2.2.
9. Click **Submit** and complete 2FA authentication.
10. **Keep the API Key and Secret available** for the installer wizard in Step 5.

---

## 4. Linux Kernel Low-Latency & Network Tuning

Connect to your GCP instance:
```bash
gcloud compute ssh $INSTANCE_NAME --zone=$ZONE
```

Apply high-throughput, low-latency TCP stack configurations:

```bash
sudo bash -c 'cat << "EOF" > /etc/sysctl.d/99-trading-latency.conf
# TCP Low Latency & Socket Buffers
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# Enable TCP Fast Open (client & server)
net.ipv4.tcp_fastopen = 3

# Disable slow start on idling connections
net.ipv4.tcp_slow_start_after_idle = 0

# Enable TCP BBR Congestion Control
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Keep-alive intervals for active WebSockets
net.ipv4.tcp_keepalive_time = 30
net.ipv4.tcp_keepalive_intvl = 5
net.ipv4.tcp_keepalive_probes = 3

# File Descriptors Limit
fs.file-max = 2097152
EOF'

sudo sysctl --system
```

Increase system file descriptor limits:
```bash
sudo bash -c 'cat << "EOF" >> /etc/security/limits.conf
* soft nofile 65535
* hard nofile 65535
EOF'
```

---

## 5. Zero-Touch Deployment & Interactive Setup

Clone the repository and run the zero-touch installer:

```bash
git clone https://github.com/webdotpulse/Mybit.git
cd Mybit

# Run the automated installer
./install.sh
```

### What the installer handles automatically:
1. Verifies Python 3.11+ runtime.
2. Creates an isolated `.venv` environment and installs all dependencies (`aiohttp`, `websockets`, `pydantic`, `cryptography`, `rich`, `numpy`).
3. Prompts for Bybit API Key & Secret (masked input).
4. Asks for Mode (Testnet vs. Live Mainnet), Allocated Capital, and Risk % limits.
5. Encrypts your API credentials using Fernet encryption into `secrets.enc` with `chmod 600`.
6. Generates and registers the `systemd` daemon: `bybit-engine.service`.

---

## 6. Latency Verification Benchmark

Verify that your network connection to Bybit Singapore matches the sub-10ms requirement:

```bash
.venv/bin/python3 -c "
import asyncio, time, aiohttp

async def test_latency():
    async with aiohttp.ClientSession() as session:
        latencies = []
        for i in range(5):
            t0 = time.perf_counter()
            async with session.get('https://api.bybit.com/v5/market/time') as resp:
                await resp.json()
                latencies.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.2)
        print(f'Average Bybit REST Latency: {sum(latencies)/len(latencies):.2f} ms')
        print(f'Minimum Round-Trip Time:   {min(latencies):.2f} ms')

asyncio.run(test_latency())
"
```

*Expected result in GCP Singapore (`asia-southeast1`): 3.5ms – 8.5ms average latency.*

---

## 7. Operations & Management Commands

Use the companion script `./manage.sh` for all daily operations:

### 1. View Live Monitor Dashboard
```bash
./manage.sh status
```
*Displays account equity, drawdown, active circuit breakers, open positions with unrealized PnL, win rate, and profit factor.*

### 2. Stream Live Execution Logs
```bash
./manage.sh logs
```

### 3. Emergency Panic Stop
```bash
./manage.sh panic
```
*Instantly cancels all active limit orders and sends Market Reduce-Only orders to flatten all open positions.*

### 4. Zero-Downtime Code Update
```bash
./manage.sh update
```
*Pulls latest git commits, syncs pip requirements, and restarts the systemd daemon.*

### 5. Service Daemon Control
```bash
./manage.sh start     # Start daemon
./manage.sh stop      # Stop daemon
./manage.sh restart   # Cleanly restart daemon
```

---

## 8. High-Availability & Disaster Recovery

### Automated Database Backups to Google Cloud Storage (GCS)
Create a daily cron job to back up your `trading_journal.db` to GCS:

```bash
# Create backup storage bucket
gcloud storage buckets create gs://$PROJECT_ID-bybit-backups --location=$REGION

# Add daily cron job
(crontab -l 2>/dev/null; echo "0 1 * * * cp /home/\$USER/Mybit/data/trading_journal.db /tmp/journal_\$(date +\%Y\%m\%d).db && gcloud storage cp /tmp/journal_\$(date +\%Y\%m\%d).db gs://$PROJECT_ID-bybit-backups/ && rm -f /tmp/journal_*.db") | crontab -
```

### Automatic System Recovery
The systemd service (`bybit-engine.service`) is configured with:
```ini
Restart=on-failure
RestartSec=5s
```
If an unhandled exception or network partition occurs, systemd automatically respawns the engine within 5 seconds. On startup, the engine executes `RiskManager.reconcile()` within the first second to re-sync all positions and purge any stale orders.
