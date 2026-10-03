"""The parser sandbox: Docling's document conversion in a container of its own.

``protocol`` is the wire format (standard library only, no sockets), ``server`` the service that
runs in the parser container, ``docling_converter`` the converter it serves, and ``cli`` the
``python -m procurement_agent.parser_service`` entry point. The client lives with the code that
uses it, in ``procurement_agent/ingestion/parser_client.py``. Nothing is imported here: the
container's health check starts this package every half minute and must not load Docling to do it.
"""
