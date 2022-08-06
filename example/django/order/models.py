from django.db import models

from pg_bitemporal.django.base import generate_bitemporal_tables


class OrderBase(models.Model):
    order_id = models.UUIDField()

    status = models.CharField()

    class Meta:
        abstract = True


Order, OrderHistory = generate_bitemporal_tables(
    mixin_cls=OrderBase, key_fields_and_equality_operators=[("order_id", "=")]
)
