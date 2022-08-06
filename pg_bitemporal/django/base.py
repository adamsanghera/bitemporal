from typing import List, Tuple

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields.ranges import DateTimeRangeField
from django.db import connection, models
from django.db.models.signals import post_migrate, pre_delete, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import camel_case_to_spaces


def default_range():
    """Return the default tstzrange."""
    return f"[{timezone.now()},)"


def generate_bitemporal_tables(
    mixin_cls: models.Model, key_fields_and_equality_operators: List[Tuple[str, str]]
) -> Tuple[models.Model, models.Model]:
    """Generate a current and history table from the given inheriting from `mixin_cls`."""
    class_name: str = mixin_cls.__name__
    if not class_name.endswith("Base"):
        raise ValueError(
            "Abstract class name should end with `Base` to avoid confusion"
        )

    # trim "Base"
    class_name = class_name[:-4]
    table_name = camel_case_to_spaces(class_name.lower())

    class CurrentMetaClass(models.base.ModelBase):
        def __new__(cls, name, bases, attrs):
            return super().__new__(cls, class_name, bases, attrs)

    class HistoryMetaClass(models.base.ModelBase):
        def __new__(cls, name, bases, attrs):
            return super().__new__(cls, f"{class_name}History", bases, attrs)

    class Current(mixin_cls, metaclass=CurrentMetaClass):
        """Mixin for current table, i.e. values that have not been expired.

        Unfortunately, Django does not allow server-side defaults (i.e. executed by the db).
        """

        # Primary key, because the bitemporal key is not unique irrespective of time.
        # UUID is an acceptable choice, but an incrementing integer is a stylistic choice.
        row_id = models.BigAutoField(primary_key=True)

        # time dimension relevant to the application
        app_period = DateTimeRangeField(null=False, default=default_range)

        # time dimension relevant to the "transaction" / persistence layer
        txn_period = DateTimeRangeField(null=False, default=default_range)

        class Meta:
            app_label = mixin_cls._meta.app_label
            db_table = f"{app_label}_{table_name}"

            constraints = [
                models.UniqueConstraint(
                    fields=(
                        "app_period",
                        *(
                            key_field
                            for key_field, _ in key_fields_and_equality_operators
                        ),
                    ),
                    # TODO: more django-idiomatic name convention
                    name=f"{db_table}_bitemporal_unique_key",
                ),
                ExclusionConstraint(
                    expressions=[
                        *(
                            (key_field, equality_operator)
                            for key_field, equality_operator in key_fields_and_equality_operators
                        ),
                        ("app_period", "&&"),
                    ],
                    # TODO: more django-idiomatic name convention
                    name=f"{db_table}_bitemporal_key_exclusion",
                ),
            ]

            # TODO indexes

    class History(mixin_cls, metaclass=HistoryMetaClass):
        """Base for 'history' table, i.e. values that have been expired."""

        # Primary key, because the bitemporal key is not unique irrespective of time.
        # UUID is an acceptable choice, but an incrementing integer is a stylistic choice.
        row_id = models.BigAutoField(primary_key=True)

        # time dimension relevant to the application
        app_period = DateTimeRangeField(null=False, default=default_range)

        # the time dimension relevant to the "transaction" i.e. persistence in `current`
        # this period joins the PK with `row_id`, because there can be duplicate `row_id`
        # keys in the history table; otherwise, it wouldn't be a very useful history table!
        txn_period = DateTimeRangeField(null=False, default=default_range)

        class Meta:
            app_label = mixin_cls._meta.app_label
            db_table = f"{app_label}_{table_name}_history"

            constraints = [
                # NOTE: the only difference here is the absence of a `unique_constraint`,
                # since the history table can include rows with duplicate key/`app_period`.
                # rows with identical bitemporal-key values can overlap on `app_period`
                ExclusionConstraint(
                    expressions=[
                        *(
                            (key_field, equality_operator)
                            for key_field, equality_operator in key_fields_and_equality_operators
                        ),
                        ("app_period", "&&"),
                    ],
                    name=f"{db_table}_bitemporal_key_exclusion",
                ),
                models.UniqueConstraint(
                    fields=("row_id", "txn_period"),
                    name=f"{db_table}_composite_key",
                ),
            ]

    @receiver(signal=[pre_save, pre_delete], sender=History)
    def history_mutation_preventer(sender, **kwargs):
        raise ValueError(f"May not mutate instances of {sender.__class__}")

    @receiver(signal=[post_migrate], sender=History)
    def history_trigger_emitter(sender, **kwargs):
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TRIGGER versioning_trigger "
                f'BEFORE INSERT OR DELETE OR UPDATE ON "{sender._meta.db_table}" '
                "FOR EACH ROW EXECUTE PROCEDURE "
                f"public.record_history('{sender._meta.db_table}')"
            )

    return Current, History
