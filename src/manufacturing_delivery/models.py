"""制造交付编排领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 衡阳基地同时交付的两大类输变电装备。
EQUIPMENT_TYPES = {"transformer", "switchgear"}

# 部件按件跟踪，不设小数。
COMPONENT_CATEGORIES = {
    "core",            # 铁芯（变压器）
    "winding",         # 绕组（变压器）
    "bushing",         # 套管
    "tap_changer",     # 有载分接开关
    "tank",            # 油箱
    "breaker",         # 断路器（成套开关设备）
    "busbar",          # 母线
    "relay",           # 保护继电器
    "enclosure",       # 柜体
    "other",
}

# 批次质量状态只能沿准入方向推进。
LOT_STATES = {"quarantined", "released", "rejected"}

# 工程变更影响范围：
# - none：变更不影响在制设备；
# - unstarted_only：已开工设备锁定原设计，未开工部分可重新排程；
# - all：所有设备都必须重评，已开工设备需要强制让步（本服务拒绝静默换型）。
CHANGE_SCOPES = {"none", "unstarted_only", "all"}

# 检验关口结论。
INSPECTION_RESULTS = {"pending", "passed", "failed", "waived"}

# 运输窗口状态。
SHIPMENT_STATES = {"open", "closed", "departed"}


def required_text(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field_name} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field_name} 格式不正确")
    return result


def decimal_value(
    value: object,
    field_name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field_name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field_name} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field_name} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field_name} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field_name} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field_name} 必须是正整数")
    return value


def non_negative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{field_name} 必须是非负整数")
    return value


def date_text(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field_name} 必须是 YYYY-MM-DD 日期") from exc


def timestamp_text(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 40)
    try:
        return parse_utc(result, field_name).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def choice(value: object, field_name: str, choices: set[str]) -> str:
    result = required_text(value, field_name, 32)
    if result not in choices:
        raise ValidationFailed(f"{field_name} 必须是 {sorted(choices)} 之一")
    return result


@dataclass(frozen=True, slots=True)
class EquipmentModel:
    """可交付装备型号（变压器 / 成套开关设备）。"""

    model_id: str
    equipment_type: str
    name: str
    bom: Mapping[str, int]  # 部件类别 -> 每台用量
    preferred_components: Mapping[str, str]  # 部件类别 -> 首选部件型号
    routing_hours: Mapping[str, Decimal]  # 工位 -> 每台标准工时
    inspection_gates: tuple[str, ...]  # 必须依次通过的检验关口
    accepts_substitutes: bool  # 客户约束默认值：是否允许替代部件

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EquipmentModel":
        equipment_type = choice(raw.get("equipment_type"), "equipment_type", EQUIPMENT_TYPES)
        bom_raw = raw.get("bom")
        if not isinstance(bom_raw, Mapping) or not bom_raw:
            raise ValidationFailed("bom 必须是非空对象")
        bom: dict[str, int] = {}
        for key, amount in bom_raw.items():
            category = required_text(key, "bom 键", 32)
            if category not in COMPONENT_CATEGORIES:
                raise ValidationFailed(f"bom.{key} 不是受支持的部件类别")
            bom[category] = positive_integer(amount, f"bom.{key}")
        preferred_raw = raw.get("preferred_components", {})
        if not isinstance(preferred_raw, Mapping):
            raise ValidationFailed("preferred_components 必须是对象")
        if set(preferred_raw) != set(bom):
            raise ValidationFailed("preferred_components 的键必须与 bom 的部件类别完全一致")
        preferred_components = {
            category: identifier(preferred_raw[category], f"preferred_components.{category}")
            for category in bom
        }
        routing_raw = raw.get("routing_hours")
        if not isinstance(routing_raw, Mapping) or not routing_raw:
            raise ValidationFailed("routing_hours 必须是非空对象")
        routing = {
            identifier(station, "routing_hours 键"): decimal_value(
                hours, f"routing_hours.{station}", minimum=Decimal("0.01")
            )
            for station, hours in routing_raw.items()
        }
        gates_raw = raw.get("inspection_gates")
        if not isinstance(gates_raw, list) or not gates_raw:
            raise ValidationFailed("inspection_gates 必须是非空数组")
        if len(set(gates_raw)) != len(gates_raw):
            raise ValidationFailed("inspection_gates 不能重复")
        gates = tuple(identifier(item, "inspection_gates 项") for item in gates_raw)
        return cls(
            model_id=identifier(raw.get("model_id"), "model_id"),
            equipment_type=equipment_type,
            name=required_text(raw.get("name"), "name"),
            bom=bom,
            preferred_components=preferred_components,
            routing_hours=routing,
            inspection_gates=gates,
            accepts_substitutes=bool(raw.get("accepts_substitutes", True)),
        )


@dataclass(frozen=True, slots=True)
class ComponentLot:
    """关键部件批次：按型号、质量等级、可用数量跟踪。"""

    lot_id: str
    component_model: str
    category: str
    quality_grade: str
    quantity: int
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ComponentLot":
        return cls(
            lot_id=identifier(raw.get("lot_id"), "lot_id"),
            component_model=identifier(raw.get("component_model"), "component_model"),
            category=choice(raw.get("category"), "category", COMPONENT_CATEGORIES),
            quality_grade=required_text(raw.get("quality_grade"), "quality_grade", 32).upper(),
            quantity=positive_integer(raw.get("quantity"), "quantity"),
            received_at=timestamp_text(raw.get("received_at"), "received_at"),
        )


@dataclass(frozen=True, slots=True)
class SubstituteRule:
    """替代部件准入规则：客户约束 + 质量规则同时满足才可进入候选。"""

    rule_id: str
    equipment_type: str
    category: str
    preferred_model: str
    substitute_model: str
    allow_customer_override: bool
    minimum_grade_rank: int  # 等级数字越小越好（A=1）
    note: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubstituteRule":
        preferred = identifier(raw.get("preferred_model"), "preferred_model")
        substitute = identifier(raw.get("substitute_model"), "substitute_model")
        if preferred == substitute:
            raise ValidationFailed("替代部件型号不能与首选型号相同")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            equipment_type=choice(raw.get("equipment_type"), "equipment_type", EQUIPMENT_TYPES),
            category=choice(raw.get("category"), "category", COMPONENT_CATEGORIES),
            preferred_model=preferred,
            substitute_model=substitute,
            allow_customer_override=bool(raw.get("allow_customer_override", True)),
            minimum_grade_rank=positive_integer(raw.get("minimum_grade_rank", 1), "minimum_grade_rank"),
            note=str(raw.get("note", "") or "").strip()[:256],
        )


@dataclass(frozen=True, slots=True)
class LineCapacity:
    """产线工位在某个生产日期的能力（工时）。"""

    line_id: str
    station: str
    service_date: str
    available_hours: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LineCapacity":
        return cls(
            line_id=identifier(raw.get("line_id"), "line_id"),
            station=identifier(raw.get("station"), "station"),
            service_date=date_text(raw.get("service_date"), "service_date"),
            available_hours=decimal_value(
                raw.get("available_hours"), "available_hours", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class ShippingWindow:
    """运输窗口：设备必须在窗口内完成检验并发运。"""

    window_id: str
    destination: str
    opens_on: str
    closes_on: str
    capacity_units: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ShippingWindow":
        opens_on = date_text(raw.get("opens_on"), "opens_on")
        closes_on = date_text(raw.get("closes_on"), "closes_on")
        if closes_on < opens_on:
            raise ValidationFailed("closes_on 不能早于 opens_on")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            destination=required_text(raw.get("destination"), "destination", 64),
            opens_on=opens_on,
            closes_on=closes_on,
            capacity_units=positive_integer(raw.get("capacity_units"), "capacity_units"),
        )


@dataclass(frozen=True, slots=True)
class OrderItem:
    model_id: str
    quantity: int
    allow_substitutes: bool  # 客户约束（合同条款）
    required_grades: Mapping[str, str]  # 部件类别 -> 最低质量等级（字母）


@dataclass(frozen=True, slots=True)
class CustomerOrder:
    """客户合同订单：含承诺交期、运输窗口与客户约束。"""

    order_id: str
    customer: str
    items: tuple[OrderItem, ...]
    due_date: str
    window_id: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CustomerOrder":
        items_raw = raw.get("items")
        if not isinstance(items_raw, list) or not items_raw:
            raise ValidationFailed("items 必须是非空数组")
        items: list[OrderItem] = []
        seen_models: set[str] = set()
        for entry in items_raw:
            if not isinstance(entry, Mapping):
                raise ValidationFailed("items 项必须是对象")
            model_id = identifier(entry.get("model_id"), "model_id")
            if model_id in seen_models:
                raise ValidationFailed(f"订单中型号 {model_id} 重复")
            seen_models.add(model_id)
            grades_raw = entry.get("required_grades", {})
            if not isinstance(grades_raw, Mapping):
                raise ValidationFailed("required_grades 必须是对象")
            required_grades = {
                choice(key, "required_grades 键", COMPONENT_CATEGORIES): required_text(
                    value, f"required_grades.{key}", 8
                ).upper()
                for key, value in grades_raw.items()
            }
            items.append(
                OrderItem(
                    model_id=model_id,
                    quantity=positive_integer(entry.get("quantity"), "quantity"),
                    allow_substitutes=bool(entry.get("allow_substitutes", False)),
                    required_grades=required_grades,
                )
            )
        return cls(
            order_id=identifier(raw.get("order_id"), "order_id"),
            customer=required_text(raw.get("customer"), "customer", 64),
            items=tuple(items),
            due_date=date_text(raw.get("due_date"), "due_date"),
            window_id=identifier(raw.get("window_id"), "window_id"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class EngineeringChange:
    """工程变更：生效后约束在制设备能否换型。"""

    change_id: str
    model_id: str
    revision_label: str
    effective_on: str
    scope: str
    overrides: Mapping[str, str]  # 部件类别 -> 新首选部件型号
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EngineeringChange":
        overrides_raw = raw.get("overrides", {})
        if not isinstance(overrides_raw, Mapping):
            raise ValidationFailed("overrides 必须是对象")
        overrides = {
            choice(key, "overrides 键", COMPONENT_CATEGORIES): identifier(
                value, f"overrides.{key}"
            )
            for key, value in overrides_raw.items()
        }
        return cls(
            change_id=identifier(raw.get("change_id"), "change_id"),
            model_id=identifier(raw.get("model_id"), "model_id"),
            revision_label=required_text(raw.get("revision_label"), "revision_label", 32),
            effective_on=date_text(raw.get("effective_on"), "effective_on"),
            scope=choice(raw.get("scope", "unstarted_only"), "scope", CHANGE_SCOPES),
            overrides=overrides,
            reason=required_text(raw.get("reason"), "reason"),
        )
