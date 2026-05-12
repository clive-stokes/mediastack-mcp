# get_iplayer Integration

get_iplayer has no API, but it supports a `--command` option that runs a
shell command after each successful download. Use this to push events into
MediaStack via the `/ingest` endpoint.

## Single download

From within the Docker network (e.g. another container on `npm-network`),
use port **8000** (the container port). From the host, use port **9202**.

```bash
get_iplayer --pid b01234567 \
  --command 'curl -s -X POST http://mediastack-mcp:8000/ingest \
    -H "Content-Type: application/json" \
    -d "{\"source\":\"get_iplayer\",\"event_type\":\"downloaded\",\"title\":\"<name>\",\"metadata\":{\"pid\":\"<pid>\",\"type\":\"<type>\",\"filename\":\"<filename>\",\"dir\":\"<dir>\",\"series\":\"<series>\",\"episode\":\"<episode>\"}}"'
```

From the NAS host directly:

```bash
curl -s -X POST http://localhost:9202/ingest \
  -H "Content-Type: application/json" \
  -d '{"source":"get_iplayer","event_type":"downloaded","title":"Programme Name","metadata":{"pid":"b01234567"}}'
```
