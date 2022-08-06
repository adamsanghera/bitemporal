from sqlalchemy import Column, create_engine
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import scoped_session, sessionmaker

from pg_bitemporal.sqlalchemy.base import CurrentBase, HistoryBase, to_history_table

engine = create_engine(
    "postgresql://example:example@localhost:5432/example", convert_unicode=True
)
db_session = scoped_session(
    sessionmaker(autocommit=False, autoflush=False, bind=engine)
)


class OrderMixin:
    key_fields_and_equality_operators = [("order_id", "=")]
    order_id = Column(UUID)

    def __repr__(self):
        return f"row: {self.row_id}, order: {self.order_id}, app: {self.app_period}, txn: {self.txn_period}"


class Order(OrderMixin, CurrentBase):
    pass


OrderHistory = to_history_table(current_model_cls=Order, mixin_classes=[OrderMixin])

CurrentBase.metadata.create_all(engine)
HistoryBase.metadata.create_all(engine)
