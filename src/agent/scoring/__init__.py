"""Scoring live coins with the saved models (spec 15.3, step L1). Analysis only.

Nothing in this package may import the labels, the dataset or the training code
(`agent.labels`, `agent.dataset`, `agent.models.frame`, `agent.models.train`): the engine
loads it, and the engine must never be able to read an outcome. Tests check this, both
by reading the imports and by loading the engine in a fresh Python.
"""
