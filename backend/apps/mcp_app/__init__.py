"""MCP (Model Context Protocol) server for opencuria.

Exposes the same functionality as the REST API through the MCP interface.
Authentication via API key (kai_... token in Authorization header or X-API-Key).
Fine-grained permissions are enforced per-tool based on the API key's permission list.
"""
