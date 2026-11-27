"""按 Deployment 的当前 revision 和 UID 识别该次变更的 Pod。"""

import json
import sys

items = json.load(sys.stdin)["items"]
deployment = next(
    item
    for item in items
    if item["kind"] == "Deployment" and item["metadata"]["name"] == "currencyservice"
)
revision_key = "deployment.kubernetes.io/revision"
revision = deployment["metadata"].get("annotations", {}).get(revision_key)


def controlled_by(item, uids):
    return any(
        ref.get("controller") and ref["uid"] in uids
        for ref in item["metadata"].get("ownerReferences", [])
    )


replicasets = {
    item["metadata"]["uid"]
    for item in items
    if item["kind"] == "ReplicaSet"
    and revision
    and item["metadata"].get("annotations", {}).get(revision_key) == revision
    and controlled_by(item, {deployment["metadata"]["uid"]})
}
pods = []
for item in items:
    if (
        item["kind"] != "Pod"
        or item["metadata"].get("deletionTimestamp")
        or not controlled_by(item, replicasets)
    ):
        continue
    for container in item.get("status", {}).get("containerStatuses", []):
        if container["name"] == "server":
            terminated = container.get("lastState", {}).get("terminated", {})
            pods.append(
                {
                    "name": item["metadata"]["name"],
                    "uid": item["metadata"]["uid"],
                    "restarts": container["restartCount"],
                    "ready": container["ready"],
                    "running": "running" in container.get("state", {}),
                    "last_reason": terminated.get("reason"),
                }
            )
print(json.dumps(pods))
