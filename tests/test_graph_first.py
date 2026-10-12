"""End-to-end Java graph-first regressions, without a Copilot account."""
import json
from pathlib import Path

import pytest

from graphify.graph_first import ensure_graph, repository_context
from graphify.serve import _load_graph
from graphify.copilot import install, hook
from graphify.api_docs import generate


@pytest.fixture
def repo(tmp_path):
    sources = {
        "AddonController.java": '''@RestController class AddonController {
            AddonService service;
            @GetMapping("/v1/remote/addons/details") AddonResponse details() {
                return service.details();
            }
        }''',
        "AddonService.java": '''@Service class AddonService {
            AddonRepository repository;
            AddonResponse details() { return repository.load(); }
        }''',
        "AddonRepository.java": '''@Repository class AddonRepository {
            AddonResponse load() { return new AddonResponse(); }
        }''',
        "AddonResponse.java": "class AddonResponse { String name; }",
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    return tmp_path


def test_api_dependencies_and_impact(repo):
    status = ensure_graph(repo)
    graph = _load_graph(status["graph_path"])
    result = repository_context(graph, "Explain GET /v1/remote/addons/details")
    assert {"AddonController.java", "AddonService.java", "AddonRepository.java"} <= set(result["candidate_files"])
    assert "details" in result["context"]
    deps = repository_context(graph, "Which classes depend on AddonService?")
    assert "AddonController.java" in deps["candidate_files"]
    impact = repository_context(graph, "Add addonStatus to GET /v1/remote/addons/details", impact=True)
    assert "AddonResponse.java" in impact["candidate_files"]
    assert any(e["relation"] == "calls" for e in impact["relationships"])


def test_reuse_refresh_add_delete_invalid(repo):
    status = ensure_graph(repo)
    path = Path(status["graph_path"])
    stamp = path.stat().st_mtime_ns
    assert not ensure_graph(repo)["refreshed"]
    assert path.stat().st_mtime_ns == stamp
    (repo / "AddonResponse.java").write_text("class AddonResponse { String addonStatus; }", encoding="utf-8")
    assert ensure_graph(repo)["refreshed"]
    assert "addonStatus" in path.read_text(encoding="utf-8")
    (repo / "Extra.java").write_text("class Extra {}", encoding="utf-8")
    assert ensure_graph(repo)["refreshed"]
    (repo / "Extra.java").unlink()
    assert ensure_graph(repo)["refreshed"]
    assert not any(d.get("source_file") == "Extra.java" for _, d in _load_graph(str(path)).nodes(data=True))
    path.write_text("broken JSON", encoding="utf-8")
    assert ensure_graph(repo)["refreshed"]
    path.unlink()
    assert ensure_graph(repo)["refreshed"]


def test_hook_order_and_sync(repo):
    assert hook(repo, "PreToolUse", {"session_id": "new"})["continue"] is False
    payload = {"session_id": "one", "prompt": "Add addonStatus to GET /v1/remote/addons/details"}
    assert hook(repo, "UserPromptSubmit", payload)["continue"]
    result = hook(repo, "PreToolUse", {"session_id": "one"})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "AddonResponse.java" in context
    (repo / "AddonResponse.java").write_text("class AddonResponse { String addonStatus; }", encoding="utf-8")
    assert hook(repo, "Stop", {"session_id": "one"})["continue"]
    assert "addonStatus" in (repo / "graphify-out/graph.json").read_text(encoding="utf-8")


def test_configuration_preserves_existing_and_is_idempotent(repo):
    config = repo / ".vscode/mcp.json"
    config.parent.mkdir()
    config.write_text('// Existing server\n' + json.dumps({"servers": {"other": {"command": "other"}}}), encoding="utf-8")
    install(repo, "vscode")
    install(repo, "vscode")
    assert "other" in json.loads(config.read_text())["servers"]
    assert (repo / ".github/copilot-instructions.md").read_text().count("graphify-first:start") == 1
    assert "PreToolUse" in json.loads((repo / ".github/hooks/graphify.json").read_text())["hooks"]
    install(repo, "intellij")
    assert "graphify" in json.loads((repo / "graphify-copilot-mcp.json").read_text())["servers"]


def test_missing_evidence_is_explicit(repo):
    graph = _load_graph(ensure_graph(repo)["graph_path"])
    result = repository_context(graph, "ZyxNonexistentSymbol")
    assert not result["candidate_files"]
    assert "missing implementation details" in result["limitations"]


def test_existing_documentation_for_requested_endpoint(repo):
    path = Path(ensure_graph(repo)["graph_path"])
    docs = generate(path, repo / "api-docs", endpoint="GET /v1/remote/addons/details")
    assert len(docs) == 2
    content = next(p for p in docs if p.name != "index.md").read_text(encoding="utf-8")
    assert "AddonService" in content
    assert "AddonRepository" in content


def test_inventory_does_not_read_source(repo, monkeypatch):
    ensure_graph(repo)
    original = Path.read_text
    def guarded(path, *args, **kwargs):
        if path.suffix == ".java":
            raise AssertionError("freshness check read source")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded)
    assert not ensure_graph(repo)["refreshed"]


def test_scope_change(repo):
    from graphify.watch import _write_build_config
    ensure_graph(repo)
    _write_build_config(repo / "graphify-out", excludes=["AddonResponse.java"])
    with pytest.raises(RuntimeError, match="scope changed"):
        ensure_graph(repo)


def test_mcp_stdio_workflow(repo):
    import asyncio
    import sys
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters

    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "graphify.serve", str(repo / "graphify-out/graph.json")])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listed = await client.list_tools()
                assert {"repository_context", "analyze_change_impact", "refresh_graph", "query_graph"} <= {t.name for t in listed.tools}
                response = await client.call_tool("repository_context", {"question": "Explain GET /v1/remote/addons/details"})
                result = json.loads(response.content[0].text)
                assert result["synchronization"]["refreshed"]
                assert "AddonController.java" in result["candidate_files"]
                response = await client.call_tool("analyze_change_impact", {"question": "Add addonStatus to GET /v1/remote/addons/details"})
                assert "AddonResponse.java" in json.loads(response.content[0].text)["candidate_files"]
                (repo / "AddonResponse.java").write_text("class AddonResponse { String addonStatus; }", encoding="utf-8")
                response = await client.call_tool("refresh_graph", {})
                assert json.loads(response.content[0].text)["refreshed"]
                assert "addonStatus" in (repo / "graphify-out/graph.json").read_text(encoding="utf-8")
                response = await client.call_tool("query_graph", {"question": "AddonResponse"})
                assert "AddonResponse" in response.content[0].text
    asyncio.run(run())
