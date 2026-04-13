# Troubleshooting Guide

When operating a distributed edge AI ecosystem natively over Docker boundaries, there are a few established execution limits and misconfigurations that will trigger failures. This guide lists the most common issues and exactly how to resolve them definitively.

## 1. Connection Refused on `flume start`

**Symptom**: The Go CLI spins up your containers natively, but the Worker Nodes repeatedly fail to inject keys, printing `connection refused`.

**Root Cause**: OpenBao requires ~5 seconds to bootstrap effectively within the orchestration layer. While the Go CLI leverages native `Sleep()` logic loops delaying execution for 10 seconds, ultra-slow Mac machines or heavily degraded host environments might time out before the OpenBao initialization threshold.

**Resolution**:
1. Execute the Annihilation Protocol: `flume destroy`
2. Simply restart the environment via the CLI again: `flume start`

> [!TIP]
> Do not attempt to manually `docker restart flume-openbao`. The Go CLI performs native unseal mapping injection; manual restarts will break the environment state securely.

## 2. Elasticsearch Memory Mapping Crashes

**Symptom**: The Elasticsearch container logs `max virtual memory areas vm.max_map_count [65530] is too low, increase to at least [262144]`.

**Root Cause**: Elasticsearch requires massive allocations of memory mappings to support the Elastro ATS codebase indexing vectors. Default Linux configurations limit this below the operational floor. (This issue is primarily absent on MacOS OrbStack since the Docker daemon manages the VM map dynamically).

**Resolution (Raw Linux)**:
You must explicitly configure your `sysctl` kernel boundary native to your host machine:

```bash
sudo sysctl -w vm.max_map_count=262144
```

To permanently solve this against reboots, append it natively into your `.conf` array:
```bash
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
```

## 3. Lingering Port Collisions (Dashboard/Node Failing)

**Symptom**: `flume start` reports successful boots, but `localhost:8765` is hanging indefinitely or throws a blank port error. Output inside the Flume container states `address already in use`.

**Root Cause**: Often, if you `Ctrl+C` the terminal forcefully during a `flume start` sequence without properly terminating the backend dashboard daemon (`uvicorn`), a zombie process persists inside the network bridge locally.

**Resolution**:
1. Execute `flume doctor` safely. This runs native `lipgloss` telemetry arrays to pinpoint exactly which UI process is eating the port.
2. If `flume doctor` confirms the mismatch, forcefully wipe the container bridge natively: `flume destroy`
3. Fallback (Native Bash): `sudo lsof -i :8765` and `kill -9 <PID>` to nuke the parent daemon out of existence.

## 4. Telemetry UI Rendering Collisions (Black Screen)

**Symptom**: Navigating to the **Advanced Telemetry** monitoring pane entirely locks the React interface rendering a persistent visual void (black screen) preventing manual intervention.

**Root Cause**: When the underlying `/metrics` Go Gateway export layer natively disconnects or disables, returning a raw boolean zero-payload (`{}`), React hooks natively iterate through non-existent structural arrays without graceful fallback bounds inherently crashing the DOM map.

**Resolution**:
The underlying code strictly enforces proper `HTTP 503` proxies via the API translator securely bounding the React pipeline. Ensure your Flume instance is synchronized across orchestrators:
1. Reload the Flume matrix gracefully executing `./flume restart` to resync the Gateway payload loops correctly. 
2. If the UI still throws catastrophic render faults securely hard-refresh your browser clearing stale structural component caches seamlessly. 
