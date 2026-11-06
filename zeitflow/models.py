from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Role(StrEnum):
    EMPLOYEE = "employee"
    FOREMAN = "foreman"
    ADMIN = "admin"


class UserStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class User:
    id: int
    telegram_id: int
    name: str
    lang: str
    role: Role
    status: UserStatus

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE

    @property
    def can_enter_for_others(self) -> bool:
        return self.role in (Role.FOREMAN, Role.ADMIN)

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN


@dataclass(frozen=True)
class Customer:
    id: int
    name: str
    active: bool


@dataclass(frozen=True)
class Project:
    id: int
    customer_id: int
    customer_name: str
    name: str
    cost_center: str
    active: bool


@dataclass(frozen=True)
class Entry:
    id: int
    user_id: int
    user_name: str
    project_id: int
    project_name: str
    customer_id: int
    customer_name: str
    cost_center: str
    work_date: date
    start_time: str
    end_time: str
    break_minutes: int
    work_minutes: int
    activity_original: str
    activity_de: str | None
    source_lang: str
    created_by: int

    @property
    def hours(self) -> float:
        return self.work_minutes / 60
