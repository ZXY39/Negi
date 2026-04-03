from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .state import CeoGraphState


def build_langgraph_ceo_graph(runner):
    graph = StateGraph(CeoGraphState)
    graph.add_node("prepare_turn", runner._graph_prepare_turn)
    graph.add_node("call_model", runner._graph_call_model)
    graph.add_node("normalize_model_output", runner._graph_normalize_model_output)
    graph.add_node("execute_tools", runner._graph_execute_tools)
    graph.add_node("finalize_turn", runner._graph_finalize_turn)
    graph.add_edge(START, "prepare_turn")
    graph.add_edge("prepare_turn", "call_model")
    graph.add_edge("call_model", "normalize_model_output")
    graph.add_conditional_edges(
        "normalize_model_output",
        runner._graph_next_step,
        {
            "call_model": "call_model",
            "execute_tools": "execute_tools",
            "finalize": "finalize_turn",
        },
    )
    graph.add_edge("execute_tools", "call_model")
    graph.add_edge("finalize_turn", END)
    return graph.compile(name="ceo-frontdoor-langgraph")
