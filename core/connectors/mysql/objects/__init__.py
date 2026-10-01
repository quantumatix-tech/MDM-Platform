"""MySQL object modules.

Each module owns the discovery and creation logic for one MySQL object
type. Object modules must not import ``source``, ``target`` or ``cdc``;
they depend only on ``core.connectors.mysql._models``,
``core.connectors.mysql._schema``, the shared connector DTOs and the
standard library.
"""
