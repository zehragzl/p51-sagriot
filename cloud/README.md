# Cloud

Runs on the cloud VM (now: Azure, Sweden Central, B2ats_v2, Ubuntu 24.04)
with Docker. Everything the VM needs is in this folder, so moving to a new
VM is: install Docker, copy the folder, create the two secret files,
`docker compose up -d`.

| File | What |
|---|---|
| `docker-compose.yml` | broker (Mosquitto) + cloud service |
| `mosquitto/mosquitto.conf` | broker config, password required |
| `mosquitto/passwd` | broker users (not committed, created on the VM) |
| `.env` | user/password for the cloud service (not committed) |
| `fake_cloud.py` | stand-in service: answers each reading with model "echo" |

## Setup on a new VM

```bash
# from the Mac
scp -r cloud azureuser@<IP>:~/
# on the VM
cd ~/cloud
cp .env.example .env && nano .env                     # set the password
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2 \
  mosquitto_passwd -c -b /m/passwd sagriot '<password>'
sudo chown 1883:1883 mosquitto/passwd   # the broker runs as uid 1883
docker compose up -d
docker compose logs -f
```

Create `mosquitto/passwd` before the first `docker compose up`; otherwise
Docker creates an empty directory with that name.

Port 1883 must be open in the VM's network security group.
The connection is not encrypted yet (TLS is the next step).
