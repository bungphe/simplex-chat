"""Example plugin: a custom skill backed by your own system.

Replace the dictionary with a call to your order database or API.
"""

from ai_employees.skills import SkillContext, SkillError, skill

ORDERS = {
    "DH-1001": "Đã giao thành công ngày 20/09/2026",
    "DH-1002": "Đang vận chuyển, dự kiến giao 27/09/2026",
}


@skill(
    "order_status",
    "Look up the delivery status of an order by its order number (format DH-xxxx).",
    {"order_id": {"type": "string", "description": "Order number, e.g. DH-1001"}},
)
def order_status(ctx: SkillContext, order_id: str) -> str:
    status = ORDERS.get(order_id.strip().upper())
    if status is None:
        raise SkillError(f"order {order_id} not found")
    return f"{order_id}: {status}"
