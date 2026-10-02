"""Incident Response Demo: a presentation layer over the existing investigator (post-Iteration-7).

    browser  <-- SSE / REST -->  demo.server  -->  demo.engine  -->  investigator.* (existing, unchanged)
                                                   demo.agent    -->  LLM (read-only tools over Capabilities)

Everything shown in the page is read live from the cluster or produced by the real pipeline; nothing is simulated.
The LLM investigator agent only reads; approval, execution and verification stay deterministic (Iteration 7 code).
"""
