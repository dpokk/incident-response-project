"""The Investigator Agent: an LLM that investigates an incident by calling READ-ONLY tools over the existing
capability layer, then submits a structured report. It never changes the system; approval, execution and
verification stay in the deterministic Iteration 7 code."""
