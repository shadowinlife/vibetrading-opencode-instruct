from nano_search_mcp import api, server


def test_server_main_uses_streamable_http(monkeypatch):
    observed: dict[str, str] = {}

    def _fake_run(*, transport: str) -> None:
        observed["transport"] = transport

    monkeypatch.setattr(server.mcp, "run", _fake_run)

    server.main()

    assert observed == {"transport": "streamable-http"}


def test_server_main_accepts_stdio_transport(monkeypatch):
    observed: dict[str, str] = {}

    def _fake_run(*, transport: str) -> None:
        observed["transport"] = transport

    monkeypatch.setattr(server.mcp, "run", _fake_run)

    server.main(["--transport", "stdio"])

    assert observed == {"transport": "stdio"}


def test_api_app_exposes_streamable_http_route():
    paths = {route.path for route in api.app.routes}

    assert "/mcp" in paths


def test_api_main_delegates_to_server_main(monkeypatch):
    called = {"value": False}

    def _fake_server_main() -> None:
        called["value"] = True

    monkeypatch.setattr(api, "_server_main", _fake_server_main)

    api.main()

    assert called["value"] is True


def test_server_registers_all_tools():
    """确保所有对外承诺的 MCP 工具都已注册，防止新增工具时漏注册。"""
    import anyio

    tools = anyio.run(server.mcp.list_tools)
    names = {t.name for t in tools}

    expected = {
        # 通用检索
        "search",
        "fetch_page",
        "search_deferred_topic",
        # 定期报告
        "get_company_report",
        # 临时公告
        "list_announcements",
        "get_announcement_text",
        # 行业研报
        "list_industry_reports",
        "get_report_text",
        # 监管与处罚
        "list_regulatory_penalties",
        # 投资者关系
        "list_ir_meetings",
        "get_ir_meeting_text",
        # 行业政策
        "list_industry_policies",
    }
    missing = expected - names
    extra = names - expected
    assert not missing, f"缺失已承诺的 MCP 工具: {missing}"
    assert not extra, (
        f"检测到未在契约中列出的 MCP 工具 {extra}；"
        "请同步更新 tests/test_server.py 与 server.py instructions。"
    )
