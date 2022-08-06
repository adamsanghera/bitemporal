"""Helpful wrapper of SQLAlchemy.session, for interfacing with bitemporal models."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from psycopg2.extras import DateTimeTZRange
from sqlalchemy import orm, sql
from sqlalchemy.dialects.postgresql import TSTZRANGE

from pg_bitemporal.sqlalchemy.base import CurrentBase


def _get_split_record_call(schema: str, tablename: str):
    full_name = f'"{schema}"."{tablename}"' if schema is not None else f'"{tablename}"'

    return f"""SELECT split_record(NULL::{full_name}, :row_id, :app_period_1, :app_period_2)"""


class TimeRange(DateTimeTZRange):
    def contains(
        self, other_range: TimeRange | DateTimeTZRange, non_zero_gaps: bool = True
    ) -> bool:
        """Return `True` if this range contains the other range.

        This contains `other_range` if this range's boundaries are equal to or outside
        the boundaries of `other_range`. If `non_zero_gaps` are true, we require there
        to be a gap (i.e. a subtraction would result in two ranges)
        """
        if non_zero_gaps:
            l_method = (
                "__lte__" if self.lower_inc and not other_range.lower_inc else "__lt__"
            )
            u_method = (
                "__gte__" if self.upper_inc and not other_range.upper_inc else "__gt__"
            )
        else:
            l_method = (
                "__lt__" if not self.lower_inc and other_range.lower_inc else "__lte__"
            )
            u_method = (
                "__gt__" if not self.upper_inc and other_range.upper_inc else "__gte__"
            )

        return (
            (self.lower is None and other_range.lower is not None)
            or (
                self.lower is not None
                and other_range.lower is not None
                and getattr(self.lower, l_method)(other_range.lower)
            )
        ) and (
            (self.upper is None and other_range.upper is not None)
            or (
                self.upper is not None
                and other_range.upper is not None
                and getattr(self.upper, u_method)(other_range.upper)
            )
        )


class Writer:
    """Helper class for making common queries and mutations to 'Current' tables."""

    def __init__(self, model_cls, session):
        if not issubclass(model_cls, CurrentBase):
            raise ValueError(f"`model_cls` must be a child of {str(CurrentBase)}")

        self.model_cls = model_cls
        self.session = session

    def query_key(self, key: Dict[str, Any]) -> orm.query.Query:
        """Build a query into the current table of a model, given a bitemporal key."""
        query = self.session.query(self.model_cls)

        # validate bitemporal key is a complete key
        key_fields = set(
            key_field
            for key_field, _ in self.model_cls.key_fields_and_equality_operators
        )
        missing_fields = key_fields - key.keys()
        if missing_fields:
            raise ValueError(f"Missing values for bitemporal key: {missing_fields}")

        # iterate through fields, applying filters. raise on inappropriate nulls
        for key_field, _ in self.model_cls.key_fields_and_equality_operators:
            key_col = getattr(self.model_cls, key_field)

            # build the query line, and prevent unintuitive null-y problems
            if key[key_field] is None:
                if not key_col.nullable:
                    raise ValueError(f"Column {str(key_col)} may not have a null value")
                key_filter = key_col.is_(None)
            else:
                key_filter = key_col == key[key_field]

            query = query.filter(key_filter)

        return query

    def clear_key_period(self, key: Dict[str, Any], free_period: TimeRange) -> None:
        """Free up values for the given key in application-time.

        For each record that overlaps with the given bitemporal key-space, we either:
            1. Delete the record.
            2. Partition the record into two distinct application periods.
            3. Trim the application period's prefix or suffix.
        """
        # iterate through instances of the primary key that overlap with the free period
        for record in self.query_key(key=key).filter(
            self.model_cls.app_period.overlaps(free_period)
        ):
            original_period: DateTimeTZRange = record.app_period
            original_period.__class__ = TimeRange

            # record *is contained by* `free_period`: it should be deleted
            if free_period.contains(original_period):
                self.session.delete(record)
                print(f"free {free_period} contains original {original_period}")
                continue

            # record *contains* `free_period`: it should be partitioned
            # app_p : [-----)
            # free_p:   [-)
            # result: [-) [-)
            if original_period.contains(free_period):
                print(f"original {original_period} contains free {free_period}")
                self.session.execute(
                    sql.text(
                        _get_split_record_call(
                            schema=self.model_cls.__table__.schema,
                            tablename=self.model_cls.__tablename__,
                        )
                    ).bindparams(
                        app_period_1=DateTimeTZRange(
                            lower=original_period.lower,
                            upper=free_period.lower,
                            bounds=("[" if original_period.lower_inc else "(")
                            + ("]" if not free_period.lower_inc else ")"),
                        ),
                        app_period_2=DateTimeTZRange(
                            lower=free_period.upper,
                            upper=original_period.upper,
                            bounds=("[" if not free_period.upper_inc else "(")
                            + ("]" if original_period.upper_inc else ")"),
                        ),
                        row_id=record.row_id,
                    )
                )
                continue

            print(
                f"free {free_period} and original {original_period} partially overlap"
            )

            # what's left? the record must partially-overlap on the left or right.
            # app_period :   [---)
            # free_period: ----)
            # new_period :     [-)
            #
            #         ...or...
            #
            # app_period :   [---)
            # free_period:     [----
            # new_period     [-)
            record.app_period = (
                # trim prefix
                DateTimeTZRange(
                    lower=free_period.upper,
                    upper=original_period.upper,
                    bounds=(
                        ("[" if not free_period.upper_inc else "(")
                        + ("]" if original_period.upper_inc else ")")
                    ),
                )
                if (
                    free_period.lower is None
                    or (
                        original_period.lower is not None
                        and free_period.lower < original_period.lower
                    )
                )
                # trim suffix
                else DateTimeTZRange(
                    lower=original_period.lower,
                    upper=free_period.lower,
                    bounds=(
                        ("[" if original_period.lower_inc else "(")
                        + ("]" if not free_period.lower_inc else ")")
                    ),
                )
            )

        self.session.flush()
