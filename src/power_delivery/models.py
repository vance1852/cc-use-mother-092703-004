"""制造交付编排领域输入契约。

覆盖产品族（变压器 / 成套开关设备）、客户约束、部件批次、
产线能力、检验关口、运输窗口、销售订单与工程变更。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

PRODUCT_FAMILIES = {"transformer", "switchgear"}
ORDER_STATES = {"draft", "confirmed", "in_production", "change_pending", "completed", "cancelled"}
UNIT_STATES = ("planned", "production", "inspection", "awaiting_shipment", "shipped", "completed", "scrapped")


def required_text(value: object, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{name} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, name: str) -> str:
    result = required_text(value, name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{name} 格式不正确")
    return result


def positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{name} 必须是正整数")
    return value


def non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{name} 必须是非负整数")
    return value


def date_text(value: object, name: str) -> str:
    result = required_text(value, name, 10)
    from datetime import date

    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{name} 必须是 YYYY-MM-DD 日期") from exc


def identifier_set(value: object, name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, (list, tuple, set)) or any(not isinstance(item, str) for item in value):
        raise ValidationFailed(f"{name} 必须是字符串列表")
    result = frozenset(item.strip() for item in value if item.strip())
    for item in result:
        if not IDENTIFIER.fullmatch(item):
            raise ValidationFailed(f"{name} 含格式不正确的编号")
    return result


@dataclass(frozen=True, slots=True)
class CustomerConstraint:
    customer_id: str
    approved_grades: frozenset[str]
    allowed_substitutes: frozenset[str]
    notes: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CustomerConstraint":
        return cls(
            customer_id=identifier(raw.get("customer_id"), "customer_id"),
            approved_grades=identifier_set(raw.get("approved_grades"), "approved_grades"),
            allowed_substitutes=identifier_set(raw.get("allowed_substitutes"), "allowed_substitutes"),
            notes=required_text(raw.get("notes", ""), "notes", 512) if raw.get("notes") else "",
        )


@dataclass(frozen=True, slots=True)
class ComponentPart:
    part_id: str
    name: str
    family: str
    unit: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ComponentPart":
        family = required_text(raw.get("family"), "family", 32)
        if family not in PRODUCT_FAMILIES:
            raise ValidationFailed("family 必须是 transformer 或 switchgear")
        return cls(
            part_id=identifier(raw.get("part_id"), "part_id"),
            name=required_text(raw.get("name"), "name"),
            family=family,
            unit=required_text(raw.get("unit", "件"), "unit", 16),
        )


@dataclass(frozen=True, slots=True)
class SubstitutionRule:
    """工程替代关系：original 可被 substitute 替代（前提是质量等级达标）。"""

    rule_id: str
    original_part_id: str
    substitute_part_id: str
    minimum_grade: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubstitutionRule":
        original = identifier(raw.get("original_part_id"), "original_part_id")
        substitute = identifier(raw.get("substitute_part_id"), "substitute_part_id")
        if original == substitute:
            raise ValidationFailed("替代件不能与原件相同")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            original_part_id=original,
            substitute_part_id=substitute,
            minimum_grade=required_text(raw.get("minimum_grade"), "minimum_grade", 32).upper(),
        )


@dataclass(frozen=True, slots=True)
class ComponentBatch:
    batch_id: str
    part_id: str
    quantity: int
    grade: str
    heat_number: str
    certified: bool
    received_on: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ComponentBatch":
        grade = required_text(raw.get("grade"), "grade", 32).upper()
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            part_id=identifier(raw.get("part_id"), "part_id"),
            quantity=positive_int(raw.get("quantity"), "quantity"),
            grade=grade,
            heat_number=required_text(raw.get("heat_number", "-"), "heat_number", 64),
            certified=bool(raw.get("certified", False)),
            received_on=date_text(raw.get("received_on"), "received_on"),
        )


@dataclass(frozen=True, slots=True)
class ProductionLine:
    line_id: str
    family: str
    name: str
    daily_capacity: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProductionLine":
        family = required_text(raw.get("family"), "family", 32)
        if family not in PRODUCT_FAMILIES:
            raise ValidationFailed("family 必须是 transformer 或 switchgear")
        return cls(
            line_id=identifier(raw.get("line_id"), "line_id"),
            family=family,
            name=required_text(raw.get("name"), "name"),
            daily_capacity=positive_int(raw.get("daily_capacity"), "daily_capacity"),
        )


@dataclass(frozen=True, slots=True)
class InspectionGate:
    gate_id: str
    family: str
    name: str
    sequence: int
    duration_days: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InspectionGate":
        family = required_text(raw.get("family"), "family", 32)
        if family not in PRODUCT_FAMILIES:
            raise ValidationFailed("family 必须是 transformer 或 switchgear")
        return cls(
            gate_id=identifier(raw.get("gate_id"), "gate_id"),
            family=family,
            name=required_text(raw.get("name"), "name"),
            sequence=positive_int(raw.get("sequence"), "sequence"),
            duration_days=non_negative_int(raw.get("duration_days", 0), "duration_days"),
        )


@dataclass(frozen=True, slots=True)
class ShippingWindow:
    window_id: str
    destination: str
    opens_on: str
    closes_on: str
    slots: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ShippingWindow":
        opens_on = date_text(raw.get("opens_on"), "opens_on")
        closes_on = date_text(raw.get("closes_on"), "closes_on")
        if closes_on < opens_on:
            raise ValidationFailed("closes_on 不能早于 opens_on")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            destination=required_text(raw.get("destination"), "destination"),
            opens_on=opens_on,
            closes_on=closes_on,
            slots=positive_int(raw.get("slots"), "slots"),
        )


@dataclass(frozen=True, slots=True)
class BomLine:
    part_id: str
    quantity: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BomLine":
        return cls(
            part_id=identifier(raw.get("part_id"), "part_id"),
            quantity=positive_int(raw.get("quantity"), "quantity"),
        )


@dataclass(frozen=True, slots=True)
class OrderRequest:
    order_id: str
    customer_id: str
    product_model: str
    family: str
    quantity: int
    requested_date: str
    shipping_window_id: str
    bom: tuple[BomLine, ...]
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OrderRequest":
        family = required_text(raw.get("family"), "family", 32)
        if family not in PRODUCT_FAMILIES:
            raise ValidationFailed("family 必须是 transformer 或 switchgear")
        bom_raw = raw.get("bom", [])
        if not isinstance(bom_raw, list) or not bom_raw:
            raise ValidationFailed("bom 至少包含一个部件行")
        bom = tuple(BomLine.from_dict(line) for line in bom_raw)
        part_ids = [line.part_id for line in bom]
        if len(part_ids) != len(set(part_ids)):
            raise ValidationFailed("bom 中同一部件不能重复出现")
        return cls(
            order_id=identifier(raw.get("order_id"), "order_id"),
            customer_id=identifier(raw.get("customer_id"), "customer_id"),
            product_model=required_text(raw.get("product_model"), "product_model", 64),
            family=family,
            quantity=positive_int(raw.get("quantity"), "quantity"),
            requested_date=date_text(raw.get("requested_date"), "requested_date"),
            shipping_window_id=identifier(raw.get("shipping_window_id"), "shipping_window_id"),
            bom=bom,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class EngineeringChange:
    change_id: str
    order_id: str
    new_product_model: str
    new_bom: tuple[BomLine, ...]
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EngineeringChange":
        bom_raw = raw.get("new_bom", [])
        if not isinstance(bom_raw, list) or not bom_raw:
            raise ValidationFailed("new_bom 至少包含一个部件行")
        new_bom = tuple(BomLine.from_dict(line) for line in bom_raw)
        part_ids = [line.part_id for line in new_bom]
        if len(part_ids) != len(set(part_ids)):
            raise ValidationFailed("new_bom 中同一部件不能重复出现")
        return cls(
            change_id=identifier(raw.get("change_id"), "change_id"),
            order_id=identifier(raw.get("order_id"), "order_id"),
            new_product_model=required_text(raw.get("new_product_model"), "new_product_model", 64),
            new_bom=new_bom,
            reason=required_text(raw.get("reason"), "reason", 512),
        )
