from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class BaseProfileError(ValueError):
    """A local Feishu Base profile violates the allowlist contract."""


class UnregisteredResourceError(BaseProfileError):
    """A caller requested a Base resource that is not registered by alias."""


_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")
_TABLE_ID = re.compile(r"^tbl[A-Za-z0-9]+$")
_FIELD_ID = re.compile(r"^fld[A-Za-z0-9]+$")
_VIEW_ID = re.compile(r"^vew[A-Za-z0-9]+$")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BaseProfileError(f"{label} must be a non-empty string")
    return value.strip()


def _alias(value: Any, label: str) -> str:
    alias = _required_string(value, label)
    if not _ALIAS.fullmatch(alias):
        raise BaseProfileError(f"{label} is not a safe alias")
    return alias


def _resource_id(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    resource_id = _required_string(value, label)
    if not pattern.fullmatch(resource_id):
        raise BaseProfileError(f"{label} is not a valid registered ID")
    return resource_id


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BaseProfileError(f"{label} must be an object")
    return value


def _resource_aliases(
    value: Any,
    *,
    label: str,
    pattern: re.Pattern[str],
) -> Mapping[str, str]:
    raw = _mapping(value, label)
    parsed: dict[str, str] = {}
    seen_ids: set[str] = set()
    for raw_alias, raw_id in raw.items():
        alias = _alias(raw_alias, f"{label} alias")
        resource_id = _resource_id(raw_id, f"{label}.{alias}", pattern)
        if resource_id in seen_ids:
            raise BaseProfileError(f"{label} contains a duplicate registered ID")
        seen_ids.add(resource_id)
        parsed[alias] = resource_id
    return MappingProxyType(parsed)


@dataclass(frozen=True, repr=False)
class TableProfile:
    alias: str
    table_id: str
    name: str | None
    fields: Mapping[str, str] = field(repr=False)
    views: Mapping[str, str] = field(repr=False)
    user_entry_field: str | None = None

    def field_id(self, alias: str) -> str:
        try:
            return self.fields[alias]
        except KeyError as exc:
            raise UnregisteredResourceError(
                f"field alias is not registered for table {self.alias!r}: {alias!r}"
            ) from exc

    def view_id(self, alias: str) -> str:
        try:
            return self.views[alias]
        except KeyError as exc:
            raise UnregisteredResourceError(
                f"view alias is not registered for table {self.alias!r}: {alias!r}"
            ) from exc

    def __repr__(self) -> str:
        return (
            f"TableProfile(alias={self.alias!r}, "
            f"field_aliases={tuple(sorted(self.fields))!r}, "
            f"view_aliases={tuple(sorted(self.views))!r})"
        )


@dataclass(frozen=True, repr=False)
class BaseProfile:
    schema_version: str
    alias: str
    environment: str
    identity: str
    base_token: str = field(repr=False)
    base_url: str | None = field(default=None, repr=False)
    tables: Mapping[str, TableProfile] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> "BaseProfile":
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            # Never include file contents: a malformed local file may contain a token.
            raise BaseProfileError(f"could not load Base profile: {source.name}") from exc
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, value: Any) -> "BaseProfile":
        raw = _mapping(value, "profile")
        schema_version = _required_string(raw.get("schema_version"), "schema_version")
        if schema_version != "feishu_base_profile.v1":
            raise BaseProfileError("unsupported Base profile schema_version")
        environment = _alias(raw.get("environment"), "environment")
        base_alias = _alias(raw.get("base_alias", environment), "base_alias")
        identity = _required_string(raw.get("identity"), "identity").lower()
        if identity not in {"user", "bot"}:
            raise BaseProfileError("identity must be user or bot")
        base_token = _required_string(raw.get("base_token"), "base_token")
        base_url_value = raw.get("base_url")
        if base_url_value is not None and not isinstance(base_url_value, str):
            raise BaseProfileError("base_url must be a string when provided")

        raw_tables = _mapping(raw.get("tables"), "tables")
        if not raw_tables:
            raise BaseProfileError("tables must contain at least one registered alias")
        tables: dict[str, TableProfile] = {}
        seen_table_ids: set[str] = set()
        for raw_alias, raw_table in raw_tables.items():
            table_alias = _alias(raw_alias, "table alias")
            table = _mapping(raw_table, f"table {table_alias}")
            table_id = _resource_id(
                table.get("table_id"), f"table {table_alias}.table_id", _TABLE_ID
            )
            if table_id in seen_table_ids:
                raise BaseProfileError("tables contains a duplicate registered ID")
            seen_table_ids.add(table_id)
            fields = _resource_aliases(
                table.get("fields"), label=f"table {table_alias}.fields", pattern=_FIELD_ID
            )
            if not fields:
                raise BaseProfileError(f"table {table_alias!r} must register at least one field")
            views = _resource_aliases(
                table.get("views", {}), label=f"table {table_alias}.views", pattern=_VIEW_ID
            )
            name = table.get("name")
            if name is not None:
                name = _required_string(name, f"table {table_alias}.name")
            user_entry = table.get("user_entry_field")
            if user_entry is not None:
                user_entry = _alias(user_entry, f"table {table_alias}.user_entry_field")
                if user_entry not in fields:
                    raise BaseProfileError(
                        f"table {table_alias!r} user_entry_field is not a registered field alias"
                    )
            tables[table_alias] = TableProfile(
                alias=table_alias,
                table_id=table_id,
                name=name,
                fields=fields,
                views=views,
                user_entry_field=user_entry,
            )

        return cls(
            schema_version=schema_version,
            alias=base_alias,
            environment=environment,
            identity=identity,
            base_token=base_token,
            base_url=base_url_value,
            tables=MappingProxyType(tables),
        )

    def require_base(self, alias: str | None = None) -> "BaseProfile":
        requested = self.alias if alias is None else alias
        if requested != self.alias:
            raise UnregisteredResourceError(f"base alias is not registered: {requested!r}")
        return self

    def table(self, alias: str, *, base_alias: str | None = None) -> TableProfile:
        self.require_base(base_alias)
        try:
            return self.tables[alias]
        except KeyError as exc:
            raise UnregisteredResourceError(f"table alias is not registered: {alias!r}") from exc

    def field_id(
        self,
        table_alias: str,
        field_alias: str,
        *,
        base_alias: str | None = None,
    ) -> str:
        return self.table(table_alias, base_alias=base_alias).field_id(field_alias)

    def view_id(
        self,
        table_alias: str,
        view_alias: str,
        *,
        base_alias: str | None = None,
    ) -> str:
        return self.table(table_alias, base_alias=base_alias).view_id(view_alias)

    def __repr__(self) -> str:
        return (
            f"BaseProfile(alias={self.alias!r}, environment={self.environment!r}, "
            f"identity={self.identity!r}, table_aliases={tuple(sorted(self.tables))!r})"
        )
