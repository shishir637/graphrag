"""Pydantic definitions of what a valid semantic layer is. Every YAML file is loaded through these
classes, so a typo or a missing field gives a clear error instead of a wrong number later."""
import re
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, model_validator

NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _snake(v: str) -> str:
    if not NAME.match(v):
        raise ValueError(f"'{v}' must be snake_case (lowercase letters, digits, underscores)")
    return v


Name = Annotated[str, AfterValidator(_snake)]


# ---------- model.yaml ----------
class Dimension(BaseModel):
    name: Name
    column: str
    type: Literal["category", "time", "text", "number", "boolean", "key"]
    label: str = ""
    description: str = ""


class Measure(BaseModel):
    name: Name
    column: str
    agg: Literal["sum", "count", "count_distinct", "avg", "min", "max"]
    label: str = ""
    description: str = ""


class Entity(BaseModel):
    name: Name
    table: str                                  # view name in DuckDB (= file name in data/)
    label: str = ""
    description: str = ""
    primary_key: str
    dimensions: list[Dimension] = []
    measures: list[Measure] = []

    def dimension(self, name: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.name == name), None)


class Join(BaseModel):
    from_entity: str
    from_column: str
    to_entity: str
    to_column: str
    relationship: Literal["many_to_one", "one_to_one"] = "many_to_one"


class SemanticModel(BaseModel):
    version: int = 1
    entities: list[Entity]
    joins: list[Join] = []

    @model_validator(mode="after")
    def _check(self):
        names = [e.name for e in self.entities]
        if len(names) != len(set(names)):
            raise ValueError("entity names must be unique")
        for e in self.entities:
            dn = [d.name for d in e.dimensions]
            if len(dn) != len(set(dn)):
                raise ValueError(f"dimension names must be unique within entity '{e.name}'")
        for j in self.joins:
            for ent in (j.from_entity, j.to_entity):
                if ent not in names:
                    raise ValueError(f"join refers to unknown entity '{ent}'")
        return self

    def entity(self, name: str) -> Entity:
        for e in self.entities:
            if e.name == name:
                return e
        raise KeyError(f"unknown entity '{name}'")


# ---------- metrics.yaml ----------
class Metric(BaseModel):
    name: Name
    label: str
    description: str
    type: Literal["simple", "ratio", "derived", "cumulative"]
    entity: str                                 # base entity: the table the metric is counted on
    expression: str | None = None               # simple/cumulative: SQL aggregate; derived: uses {other_metric}
    numerator: str | None = None                # ratio: metric names
    denominator: str | None = None
    filters: list[str] = []                     # always-on SQL conditions, e.g. "orders.status != 'cancelled'"
    time_column: str | None = None              # entity.column used for date ranges and cumulative ordering
    format: Literal["currency", "number", "percent"] = "number"
    owner: str = ""
    status: Literal["draft", "approved"] = "draft"

    @model_validator(mode="after")
    def _by_type(self):
        if self.type in ("simple", "cumulative", "derived") and not self.expression:
            raise ValueError(f"metric '{self.name}': type {self.type} needs an expression")
        if self.type == "ratio" and not (self.numerator and self.denominator):
            raise ValueError(f"metric '{self.name}': type ratio needs numerator and denominator")
        if self.type == "cumulative" and not self.time_column:
            raise ValueError(f"metric '{self.name}': type cumulative needs a time_column")
        return self


class MetricSet(BaseModel):
    version: int = 1
    metrics: list[Metric]

    @model_validator(mode="after")
    def _unique(self):
        names = [m.name for m in self.metrics]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate metric names: {sorted(dup)}")
        return self

    def get(self, name: str) -> Metric:
        for m in self.metrics:
            if m.name == name:
                return m
        raise KeyError(f"unknown metric '{name}'")


# ---------- enrichment.yaml ----------
class Synonym(BaseModel):
    term: str                                   # everyday word: "sales", "AOV", "Bangalore"
    target: str                                 # metric name, "entity.dimension", or "entity.dimension=value"


class Enrichment(BaseModel):
    version: int = 1
    synonyms: list[Synonym] = []
    value_list_dimensions: list[str] = []       # "entity.dimension" whose distinct values are stored


# ---------- verified_queries.yaml + the request the compiler takes ----------
class Filter(BaseModel):
    dimension: str                              # "entity.dimension"
    op: Literal["=", "!=", "in", "not in", ">", ">=", "<", "<="] = "="
    values: list[str | int | float] = Field(min_length=1)


class SemanticQuery(BaseModel):
    """A structured request: the only thing the compiler accepts. The LLM fills this in;
    it never writes SQL itself."""
    metrics: list[str] = Field(min_length=1)
    dimensions: list[str] = []                  # "entity.dimension" or "entity.time_dim__month"
    filters: list[Filter] = []
    start_date: date | None = None              # inclusive, on each metric's time_column
    end_date: date | None = None                # inclusive
    order_by: list[str] = []                    # metric or dimension alias; prefix "-" for descending
    limit: int | None = None


class VerifiedQuery(BaseModel):
    question: str
    query: SemanticQuery
    verified_by: str = ""


class VerifiedSet(BaseModel):
    version: int = 1
    queries: list[VerifiedQuery]
