import pytest

from agentfw.variables import VariableError, expand, expand_all

CTX = {
    "project": {"root": "/work/proj", "name": "Demo"},
    "pack": {"spec": "${project.root}/api/*.raml", "app": "orders", "url": "https://${pack.app}.example.com"},
}


def test_expands_simple_variable():
    assert expand("${project.root}/docs", CTX) == "/work/proj/docs"


def test_expands_values_that_reference_other_variables():
    assert expand("${pack.spec}", CTX) == "/work/proj/api/*.raml"
    assert expand("${pack.url}/health", CTX) == "https://orders.example.com/health"


def test_leaves_args_for_tool_call_time():
    assert expand("run ${args.name} in ${project.root}", CTX) == "run ${args.name} in /work/proj"


def test_ignores_text_that_only_looks_like_a_variable():
    text = "password: ${secure::db.password} env=${env} ${unknown.thing}"
    assert expand(text, CTX) == text


def test_missing_variable_is_an_error():
    with pytest.raises(VariableError, match=r"\$\{project.docs_root\} is not defined"):
        expand("${project.docs_root}/**", CTX)


def test_missing_namespace_is_an_error():
    with pytest.raises(VariableError, match="no 'task' values"):
        expand("${task.workdir}", CTX)


def test_circular_reference_is_an_error():
    ctx = {"pack": {"a": "${pack.b}", "b": "${pack.a}"}}
    with pytest.raises(VariableError, match="circular"):
        expand("${pack.a}", ctx)


def test_expand_all_walks_nested_structures():
    value = {"globs": ["${project.root}/**"], "nested": {"n": "${project.name}"}, "count": 3}
    assert expand_all(value, CTX) == {"globs": ["/work/proj/**"], "nested": {"n": "Demo"}, "count": 3}
