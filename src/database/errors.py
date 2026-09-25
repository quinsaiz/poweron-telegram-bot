from sqlalchemy.exc import (
    DisconnectionError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError,
)

TRANSIENT_DATABASE_EXCEPTIONS: tuple[type[SQLAlchemyError], ...] = (
    OperationalError,
    InterfaceError,
    DisconnectionError,
    TimeoutError,
)
