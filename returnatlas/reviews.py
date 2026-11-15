"""空间聚集复核线索的不可变存档。

与研究备注分开存放：每份复核存档固化扫描参数、数据指纹、完整报告以及
报告中引用到的订单与位置原始记录，保证线索可回溯到订单、暴露与边界
假设，且源数据后续变动不会改写历史结论。
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


class ReviewStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def create(self, catalog, label, report, notes=""):
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise ValueError("线索名称需为一至一百二十个字符")
        if not isinstance(notes, str):
            raise ValueError("备注格式无效")
        referenced_orders, referenced_locations = set(), set()
        for cluster in report.get("clusters", []):
            referenced_orders.update(cluster.get("exposure_order_ids", []))
            referenced_locations.update(cluster.get("location_ids", []))
        payload = {
            "id": uuid.uuid4().hex,
            "kind": "spatial_cluster_review",
            "label": label.strip(),
            "notes": notes,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": catalog.fingerprint,
            "parameters": report["parameters"],
            "report": report,
            "evidence": {
                "deliveries": [row for row in catalog.rows("deliveries")
                               if row["id"] in referenced_orders],
                "contacts": [row for row in catalog.rows("contacts")
                             if row["delivery_id"] in referenced_orders],
                "locations": [row for row in catalog.rows("locations")
                              if row["id"] in referenced_locations],
                "service_areas": catalog.rows("service_areas"),
            },
        }
        envelope = {"content": payload,
                    "sha256": hashlib.sha256(canonical(payload)).hexdigest()}
        with (self.directory / (payload["id"] + ".json")).open("xb") as target:
            target.write(canonical(envelope))
        return payload

    def read(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise KeyError(identifier)
        try:
            envelope = json.loads((self.directory / (identifier + ".json")).read_bytes())
        except FileNotFoundError as error:
            raise KeyError(identifier) from error
        content = envelope["content"]
        if hashlib.sha256(canonical(content)).hexdigest() != envelope["sha256"]:
            raise ValueError("复核记录完整性校验失败")
        return content

    def list(self):
        return [{field: record[field] for field in
                 ("id", "label", "created_at", "source_fingerprint")}
                for record in (self.read(path.stem)
                               for path in sorted(self.directory.glob("*.json")))]
