from collections import Counter


def returned_orders(catalog):
    completed = {record["id"] for record in catalog.rows("deliveries") if record["delivered"]}
    return sorted({record["delivery_id"] for record in catalog.rows("contacts") if record["action"] == "退货" and record["delivery_id"] in completed})


def overview(catalog):
    return {"已完成单数": sum(record["delivered"] for record in catalog.rows("deliveries")), "退货订单数": len(returned_orders(catalog)), "联系记录数": len(catalog.rows("contacts")), "定位精度记录": dict(Counter(record["resolution"] for record in catalog.rows("locations")))}
