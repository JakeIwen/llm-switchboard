"""HTTP schemas projected from the stdlib records, without importing back into them."""

from dataclasses import is_dataclass
from functools import cache
from types import UnionType
from typing import NotRequired, Required, Union, get_args, get_origin, get_type_hints, is_typeddict

from pydantic import BaseModel, ConfigDict, create_model


class ResponseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


@cache
def record_model(record: type) -> type[BaseModel]:
    """Reuse a light-path record's fields, including nested records and optional keys.

    Python 3.11's stdlib TypedDict cannot be given directly to Pydantic. The
    projection also keeps the original modules entirely dependency-free.
    """
    required = getattr(record, "__required_keys__", ())
    fields = {}
    for name, annotation in get_type_hints(record, include_extras=True).items():
        default = ... if not is_typeddict(record) or name in required else None
        fields[name] = (http_type(annotation), default)
    return create_model(record.__name__, __base__=ResponseRecord, **fields)


def http_type(annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Required, NotRequired):
        return http_type(args[0])
    if origin in (Union, UnionType):
        return Union[tuple(http_type(arg) for arg in args)]
    if origin is list:
        return list[http_type(args[0])]
    if origin is dict:
        return dict[http_type(args[0]), http_type(args[1])]
    if is_typeddict(annotation) or is_dataclass(annotation):
        return record_model(annotation)
    return annotation
