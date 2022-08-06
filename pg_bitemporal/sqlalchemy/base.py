"""Root mixins and base class for bitemporal tables."""
import re
from typing import List, Tuple

from sqlalchemy import DDL, event, exc, func
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint
from sqlalchemy.orm import declarative_base, declared_attr
from sqlalchemy.schema import Index, UniqueConstraint
from sqlalchemy.sql import schema, sqltypes

get_time_period_column = lambda is_pk: schema.Column(
    TSTZRANGE,
    nullable=False,
    primary_key=is_pk,
    server_default=func.tstzrange(func.now(), None),
)


def to_snakecase(name):
    return re.sub("(?<!^)(?=[A-Z])", "_", name).lower()


class BaseMixin:
    """Base mixin for bitemporal models."""

    # The list of fields that make up the "bitemporal key" for the model, which is
    # semantically similar to a "Primary Key". This is the set of values that is unique
    # for a point in app time + txn time.
    key_fields_and_equality_operators: List[Tuple[str, str]] = []

    # Primary Key for any bitemporal model; needs to be distinct from the bitemporal
    # key, because the bitemporal key is not unique within the scope of the table.
    # UUID is acceptable, too, IMO. `row_id` is a stylistic choice.
    row_id = schema.Column(sqltypes.BIGINT, primary_key=True, autoincrement=True)


class _CurrentBase(BaseMixin):
    """Mixin for 'current' table, i.e. values that have not been expired."""

    # the time dimension relevant to the application.
    app_period = get_time_period_column(is_pk=False)

    # the time dimension relevant to the "transaction" i.e. persistence in `current`
    txn_period = get_time_period_column(is_pk=False)

    @declared_attr
    def __tablename__(cls) -> str:
        return to_snakecase(cls.__name__)

    @declared_attr
    def __table_args__(cls) -> Tuple:
        return (
            # unique constraint on (*bitmeporal_key, app_period), to allow for FKs
            UniqueConstraint(
                *(key_field for key_field, _ in cls.key_fields_and_equality_operators),
                "app_period",
                name=f"{cls.__tablename__}_bitemporal_unique_key",
            ),
            # rows with identical bitemporal-key values can overlap on `app_period`
            ExcludeConstraint(
                *(
                    (key_field, equality_operator)
                    for key_field, equality_operator in cls.key_fields_and_equality_operators
                ),
                ("app_period", "&&"),
                name=f"{cls.__tablename__}_bitemporal_key_exclusion",
            ),
            # indices for bitemporal-key
            *(
                Index(f"{cls.__tablename__}_{key_field}_idx", key_field)
                for key_field, _ in cls.key_fields_and_equality_operators
            ),
            # indices for bitemporal fields
            *(
                Index(
                    f"{cls.__tablename__}_{period}_idx", period, postgresql_using="gist"
                )
                for period in ("app_period", "txn_period")
            ),
        )


class _HistoryBase(BaseMixin):
    """Mixin for 'history' table, i.e. values that have been expired."""

    # the time dimension relevant to the application.
    app_period = get_time_period_column(is_pk=False)

    # the time dimension relevant to the "transaction" i.e. persistence in `current`
    # this period joins the PK with `row_id`, because there can be duplicate `row_id`
    # keys in the history table; otherwise, it wouldn't be a very useful history table!
    txn_period = get_time_period_column(is_pk=True)

    @declared_attr
    def __tablename__(cls) -> str:
        return to_snakecase(cls.__name__)

    @declared_attr
    def __table_args__(cls) -> Tuple:
        return (
            # NOTE: the only difference here is the absence of a `unique_constraint`,
            # since the history table can include rows with duplicate key/`app_period`.
            # rows with identical bitemporal-key values can overlap on `app_period`
            ExcludeConstraint(
                *(
                    (key_field, equality_operator)
                    for key_field, equality_operator in cls.key_fields_and_equality_operators
                ),
                ("app_period", "&&"),
                name=f"{cls.__tablename__}_bitemporal_key_exclusion",
            ),
            # indices for bitemporal-key
            *(
                Index(f"{cls.__tablename__}_{key_field}_idx", key_field)
                for key_field, _ in cls.key_fields_and_equality_operators
            ),
            # indices for bitemporal fields
            *(
                Index(
                    f"{cls.__tablename__}_{period}_idx", period, postgresql_using="gist"
                )
                for period in ("app_period", "txn_period")
            ),
        )


CurrentBase = declarative_base(cls=_CurrentBase)
HistoryBase = declarative_base(cls=_HistoryBase)


def to_history_table(current_model_cls, mixin_classes: List):
    """Generate a history table for a given 'current' table'.

    You can do all of this declaratively, but it's not recommended, because it's easy
    to make a mistake! Instead, do something like this:

    >>> class FooMixin:
    ...     key_fields_and_equality_operators = [("foo_id", "=")]
    ...     foo_id = Column(UUID)
    ...
    >>> class Foo(FooMixin, CurrentBase):
    ...     pass
    ...
    >>> generate_history_table(Foo, FooMixin)

    NOTE: This makes the assumption that the history table definition never changes
    at runtime. This assumption holds as long as `mixin_classes` and `current_model_cls`
    do not change at runtime, which should be a pretty safe assumption for most apps.
    If this is not true for you, create the history table declaratively in your model.
    """
    history_model_cls_name = f"{current_model_cls.__name__}History"
    try:
        history_model_cls = type(
            history_model_cls_name, (*(mixin_classes), HistoryBase), {}
        )
    except exc.InvalidRequestError:
        return HistoryBase.metadata.tables[to_snakecase(history_model_cls_name)]

    @event.listens_for(history_model_cls, "before_update")
    def history_update_listener(mapper, connection, target):
        raise ValueError(f"May not update {target.__table__.fullname}")

    @event.listens_for(history_model_cls, "before_insert")
    def history_insert_listener(mapper, connection, target):
        raise ValueError("May not insert into {target.__table__.fullname}")

    @event.listens_for(history_model_cls, "before_delete")
    def history_delete_listener(mapper, connection, target):
        raise ValueError("May not delete from {target.__table__.fullname}")

    history_model_cls.__table__.add_is_dependent_on(current_model_cls.__table__)
    event.listen(
        history_model_cls.__table__,
        "after_create",
        DDL(
            "CREATE TRIGGER versioning_trigger "
            f'BEFORE INSERT OR DELETE OR UPDATE ON "{current_model_cls.__tablename__}" '
            "FOR EACH ROW EXECUTE PROCEDURE "
            f"public.record_txn_history('{history_model_cls.__tablename__}')"
        ),
    )

    return history_model_cls
