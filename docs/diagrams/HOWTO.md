# Viewing diagrams, searching, and MCP

- View diagrams
  - VS Code: open any .md in this folder and use Markdown Preview (Ctrl+Shift+V).
  - Browser: double-click index.html.
- Search tools in VS Code
  - Global search: Ctrl+Shift+F. Try queries like `Sender(`, `_launch_rekey`, `HEADER_STRUCT`, `ControlState`.
  - File-only search: open a file and Ctrl+F.
  - Regex: enable .* and use patterns like `class\s+Receiver|Sender`.
- Optional MCP servers (Model Context Protocol)
  - If you use an MCP-compatible client (e.g., VS Code w/ Copilot Chat MCP), you can mount a "workspace" or "process" server to index this repo and run tasks.
  - Typical servers:
    - filesystem: browse/search files faster via MCP APIs.
    - process: run `pytest`, `python -m core.run_proxy ...` with controlled I/O.
  - This repo does not include MCP configs; if you want them, create `.mcp/servers.json` with your server definitions.

Quick links
- System: system_overview.md
- Core: core_modules.md
- Data plane: data_plane.md
- Handshake: handshake.md
- Rekey FSM: rekey_fsm.md
- Orchestration: scheduler_and_follower.md
